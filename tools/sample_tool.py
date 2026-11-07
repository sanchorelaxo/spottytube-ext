#!/usr/bin/env python3
"""Spotykach sample tool — media URL + time range -> a slot on the instrument.

Turns any yt-dlp supported URL (or a local file) plus a start/end time into a
native Spotykach WAV and puts it on the target tape. Delivery prefers USB: the
unit is asked to switch into transfer mode and the sample is written straight
to the deck. If no unit is reachable, a mounted SD card is used instead.

Why this exists: pulling a 30 s slice of a source into the Spotykach by hand is
a handful of steps that break in known ways — ranged downloads return 403, the
stale system downloader fails on signature challenges, and a WAV written with a
WAVE_FORMAT_EXTENSIBLE header loads silently as nothing. Each of those is
handled here once.

Pipeline:
    1. find a transport (USB first, SD card second) — before any download
    2. resolve the newest usable downloader, fetch the audio once, cached
    3. cut [start, end) with ffmpeg
    4. rewrite the header as plain IEEE float via wav_builder
    5. shift the tape down one slot and put the new sample in slot 1

Ordering:
    A new sample always lands in slot 1 of its tape and pushes the rest down
    1 -> 2 -> 3 ... -> 6. Nothing is overwritten: the sample that falls off the
    end is renamed with a timestamp and kept (SK/ on the card, the local archive
    directory over USB).

Deck maximum loop is 42 s, so longer segments are refused rather than silently
truncated.

Sources:
    The downloader is a parameter, not an assumption. ``--source generic``
    (default) treats the URL as an opaque identifier; ``--source youtube``
    additionally parses the video id. The pipeline is identical either way.

Usage:
    python sample_tool.py --url 'https://example.org/watch/abc' \\
        --start 6:40 --end 7:10 --deck a

    # a local file works too — same pipeline, no download
    python sample_tool.py --url /path/track.wav --start 12:45 --end 13:15 --deck b

    # what can take a sample right now (used by the GUI)
    python sample_tool.py --status --json
"""

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sk_install  # noqa: E402
import wav_builder as wb  # noqa: E402

# Deck ceiling from the manual: 42 s max loop per deck.
MAX_LOOP_SECONDS = 42.0

# Playback rates a player offers. Below 1x the stored sample is longer than the
# range you picked, so the deck ceiling can be hit from the slow side too.
MIN_SPEED = 0.25
MAX_SPEED = 2.0

# wav_builder writes a bare 44-byte canonical WAV header, no fact/ext chunks.
PLAIN_WAV_HEADER = 44

# (tape, track) each deck loads from. Both decks share tape B, so the newest
# sample sits in slot 1 for deck A and the previous one in slot 2 for deck B.
DECK_SLOTS = {True: ("B", 1), False: ("B", 2)}

#: Tape a new sample is written to unless another is named.
DEFAULT_TAPE = "B"

DEFAULT_CACHE = Path.home() / ".hermes" / "cache" / "spotykach"


class CardNotFound(RuntimeError):
    """No SD card with an SK/ folder could be located."""


class TransportMissing(RuntimeError):
    """Neither USB nor a card can take a sample right now."""


# --- sources --------------------------------------------------------------

_YOUTUBE_PATTERNS = (
    re.compile(r"(?:v=|/v/|/embed/|/shorts/|/live/)([A-Za-z0-9_-]{11})"),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
)


@dataclass(frozen=True)
class Source:
    """How to identify the thing behind a URL, and what to call it."""

    name: str
    id_patterns: tuple = ()
    description: str = ""

    def identify(self, url):
        """A stable id for this URL, or None for a local path."""
        text = str(url)
        if Path(text).exists():
            return None
        for pattern in self.id_patterns:
            match = pattern.search(text)
            if match:
                return match.group(1)
        # Anything else still needs a stable cache key; hash the URL itself so
        # an unidentified source is cached and reused like any other.
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return digest[:16]


SOURCES = {
    "generic": Source("generic", (), "any URL the downloader supports"),
    "youtube": Source("youtube", _YOUTUBE_PATTERNS, "video URLs with a parseable id"),
}

DEFAULT_SOURCE = "generic"


def resolve_source(name=None):
    """The :class:`Source` for ``name``, defaulting to generic handling."""
    key = (name or DEFAULT_SOURCE).strip().lower()
    if key not in SOURCES:
        raise ValueError(
            f"unknown source {name!r}; known sources: {', '.join(sorted(SOURCES))}"
        )
    return SOURCES[key]


def source_id(url, source=None):
    """A stable identifier for ``url`` under ``source``, or None for a file."""
    return resolve_source(source).identify(url)


@dataclass
class SampleResult:
    source: str
    source_id: str | None
    start: float
    end: float
    deck: str
    tape: str
    slot: int
    segment_path: Path
    installed: bool
    transport: str | None = None
    shifted: int = 0
    archived: Path | None = None
    card_path: Path | None = None
    speed: float = 1.0

    def as_dict(self):
        return {
            "source": self.source,
            "source_id": self.source_id,
            "start": self.start,
            "end": self.end,
            "deck": self.deck,
            "tape": self.tape,
            "slot": self.slot,
            "segment": str(self.segment_path),
            "installed": self.installed,
            "transport": self.transport,
            "shifted": self.shifted,
            "archived": str(self.archived) if self.archived else None,
            "card_path": str(self.card_path) if self.card_path else None,
            "speed": self.speed,
        }


# --- time parsing --------------------------------------------------------


def parse_time(value):
    """Parse ``SS``, ``MM:SS`` or ``HH:MM:SS`` into seconds (float)."""
    if isinstance(value, bool):
        raise ValueError(f"invalid time {value!r}")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError(f"invalid time {value!r}: negative")
        return float(value)

    text = str(value).strip()
    if not text:
        raise ValueError("invalid time '': empty")

    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"invalid time {text!r}: too many fields")

    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        raise ValueError(f"invalid time {text!r}") from None

    if any(n < 0 for n in numbers):
        raise ValueError(f"invalid time {text!r}: negative")
    # Everything but the leading field is a minutes/seconds field.
    if any(n > 59 for n in numbers[1:]):
        raise ValueError(f"invalid time {text!r}: field above 59")

    if len(numbers) == 1:
        return float(numbers[0])
    if len(numbers) == 2:
        minutes, seconds = numbers
        return float(minutes * 60 + seconds)
    hours, minutes, seconds = numbers
    return float(hours * 3600 + minutes * 60 + seconds)


def validate_speed(value):
    """Return a playback rate as a float, or raise :class:`ValueError`.

    The accepted range is what a player offers, 0.25x to 2x. ``None`` means
    normal speed, which is also what an old request without the field means.
    """
    if value is None:
        return 1.0
    if isinstance(value, bool):
        raise ValueError(f"invalid speed {value!r}")
    try:
        speed = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid speed {value!r}: not a number") from None
    if not math.isfinite(speed) or not (MIN_SPEED <= speed <= MAX_SPEED):
        raise ValueError(
            f"invalid speed {speed:g}: outside {MIN_SPEED:g}x-{MAX_SPEED:g}x"
        )
    return speed


def atempo_chain(speed):
    """ffmpeg filters that turn the source into the speed that was heard.

    ``atempo`` accepts one stage per 0.5x-2x step, so 0.25x is two halves. At
    1x nothing is added: a needless resample costs accuracy for no reason.
    """
    speed = validate_speed(speed)
    if abs(speed - 1.0) < 1e-9:
        return []
    stages = []
    remaining = speed
    while remaining < 0.5 - 1e-9:
        stages.append(0.5)
        remaining /= 0.5
    while remaining > 2.0 + 1e-9:
        stages.append(2.0)
        remaining /= 2.0
    stages.append(remaining)
    return [f"atempo={stage:.6g}" for stage in stages]


def deck_slot(is_deck_a):
    """Map deck identity to the (tape, track) slot that deck loads."""
    return DECK_SLOTS[bool(is_deck_a)]


# --- small helpers -------------------------------------------------------


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def archive_target(card, cache):
    """Where a sample that falls off a tape is kept.

    Slot 6 is not deleted: it is renamed with a timestamp and saved to SK/ on
    the card, which is where the instrument's own hierarchy lives. Over USB
    there is no filesystem to write to, so a mounted card still wins; with no
    card at all the archive falls back to the local cache directory.
    """
    if card is not None and (Path(card) / "SK").is_dir():
        try:
            if os.access(Path(card) / "SK", os.W_OK):
                return Path(card) / "SK"
        except OSError:
            pass
    return Path(cache) / "archive"


def _version_tuple(text):
    parts = re.findall(r"\d+", text)[:4]
    return tuple(int(p) for p in parts) if parts else ()


def resolve_downloader(candidates=None):
    """Pick the newest working yt-dlp.

    The distro copy is routinely months behind and fails on current signature
    challenges with an HTTP 403 on the media request, so version order decides,
    not PATH order.
    """
    if candidates is None:
        candidates = []
        found = shutil.which("yt-dlp")
        if found:
            candidates.append(Path(found))
        candidates.append(Path.home() / ".local" / "bin" / "yt-dlp")
        candidates.append(Path("/usr/local/bin/yt-dlp"))

    best, best_version = None, ()
    for candidate in candidates:
        candidate = Path(candidate) if candidate else None
        if not candidate or not candidate.exists():
            continue
        try:
            out = subprocess.run([str(candidate), "--version"],
                                 capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode != 0:
            continue
        version = _version_tuple(out.stdout.strip())
        if version and version > best_version:
            best, best_version = candidate, version

    if best is None:
        raise RuntimeError(
            "no usable yt-dlp found; install one with "
            "pip install -U yt-dlp or drop the binary in ~/.local/bin"
        )
    return best


def node_binary():
    """A JS runtime for the downloader's signature solving, or None."""
    for candidate in (shutil.which("node"),
                      Path.home() / ".hermes" / "node" / "bin" / "node",
                      Path("/usr/bin/node")):
        if candidate and Path(candidate).exists():
            return Path(candidate)
    return None


def _mounted_fat_roots():
    """Mount points of mounted vfat/exfat filesystems."""
    roots = []
    try:
        lines = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return roots
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        if fields[2] in ("vfat", "exfat", "msdos", "fat"):
            roots.append(Path(fields[1].replace("\\040", " ")))
    return roots


def find_card(roots=None):
    """Return the first root that looks like a Spotykach card (has SK/).

    Probes are guarded: system vfat mounts such as /recovery are unreadable for
    a normal user, and one PermissionError must not abort the scan.
    """
    if roots is None:
        roots = _mounted_fat_roots()
    for root in roots:
        root = Path(root)
        try:
            if (root / "SK").is_dir():
                return root
        except OSError:
            continue
    return None


def _unmounted_fat_devices():
    """Block devices holding a vfat/exfat filesystem that nothing has mounted."""
    try:
        out = subprocess.run(["lsblk", "-J", "-o", "NAME,FSTYPE,MOUNTPOINT"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    try:
        tree = json.loads(out.stdout)
    except ValueError:
        return []
    devices = []
    for node in tree.get("blockdevices", []):
        for child in node.get("children") or []:
            if child.get("fstype") in ("vfat", "exfat") and not child.get("mountpoint"):
                devices.append(Path("/dev") / child["name"])
    return devices


def autodetect_card(search_roots=None, try_mount=True):
    """Locate the card, mounting an inserted-but-unmounted one when possible."""
    if search_roots is not None:
        return find_card(roots=search_roots)

    card = find_card()
    if card is not None:
        return card
    if not try_mount:
        return None

    mounted_any = False
    for device in _unmounted_fat_devices():
        result = subprocess.run(["udisksctl", "mount", "-b", str(device)],
                                capture_output=True, text=True)
        if result.returncode == 0:
            mounted_any = True
    return find_card() if mounted_any else None


@dataclass
class CardStatus:
    """What a card root looks like right now, for `--check-card` and errors."""

    root: Path
    sk: Path
    writable: bool
    free_bytes: int
    slots: dict          # "B/1.WAV" -> bytes, or None when the slot is empty
    tapes_present: list  # tape folders that exist under SK/
    tapes_missing: list  # tape folders the firmware expects but that are absent


def card_status(root):
    """Inspect a card root without changing anything.

    `SK/` is what makes a volume the Spotykach card; the six tape folders
    (B G P R T Y) are what the firmware expects inside it. Loose slots inside
    them are normal — an empty track is just an empty track — so only the
    folders are counted as structure.
    """
    root = Path(root)
    sk = root / "SK"
    try:
        st = os.statvfs(root)
        free = st.f_bavail * st.f_frsize
        writable = not (st.f_flag & os.ST_RDONLY) and os.access(root, os.W_OK)
    except OSError:
        free, writable = 0, False

    tapes_present, tapes_missing = [], []
    for letter in sorted(wb.TAPES):
        (tapes_present if (sk / letter).is_dir() else tapes_missing).append(letter)

    slots = {}
    for tape, track in DECK_SLOTS.values():
        path = sk / tape / f"{track}.WAV"
        slots[f"{tape}/{track}.WAV"] = path.stat().st_size if path.is_file() else None
    return CardStatus(root=root, sk=sk, writable=writable, free_bytes=free,
                      slots=slots, tapes_present=tapes_present,
                      tapes_missing=tapes_missing)


def structure_problems(status):
    """Every way this card deviates from the expected layout, worst first."""
    problems = []
    if not status.sk.is_dir():
        problems.append("no SK/ folder")
    if status.sk.is_dir() and status.tapes_missing:
        problems.append("missing tape folder(s) " + " ".join(status.tapes_missing))
    if not status.writable:
        problems.append("mounted read-only")
    return problems


def blocking_problems(status):
    """Problems a write cannot fix by itself; missing tape folders are not one.

    `_install` creates absent tape folders as part of writing the slot, so only
    a missing `SK/` or a read-only mount actually stops a build. `--check-card`
    still reports the soft ones, because "is this card laid out right" and "can
    I write to it" are two different questions.
    """
    problems = []
    if not status.sk.is_dir():
        problems.append("no SK/ folder — not a Spotykach card")
    if not status.writable:
        problems.append("mounted read-only")
    return problems


def require_card(card=None, search_roots=None):
    """Return the card root or raise :class:`CardNotFound` saying why.

    Runs before any download or cut, so an unplugged card costs nothing.
    """
    if card is not None:
        root = Path(card)
        if not root.is_dir():
            raise CardNotFound(
                f"{root} is not a mounted directory. Insert the card and re-run "
                f"— the Spotykach only reads it at power-up, so the card has to "
                f"be in before you turn the unit on."
            )
        return root

    root = autodetect_card(search_roots=search_roots)
    if root is not None:
        return root

    seen = [Path(r) for r in (search_roots if search_roots is not None
                              else _mounted_fat_roots())]
    if seen:
        raise CardNotFound(
            "a card-like volume is present but none has an SK/ folder "
            f"(looked in {', '.join(str(p) for p in seen)}). Point --card at "
            "the Spotykach card's mount point."
        )
    raise CardNotFound(
        "no SD card found. Insert the Spotykach card and re-run — it is "
        "checked before anything is downloaded, and the unit only reads the "
        "card at power-up."
    )


# --- media ---------------------------------------------------------------

def ensure_source(url, cache_dir, source=None):
    """Return ``(local_path, id)``; downloads once and reuses the cache."""
    path = Path(str(url))
    if path.exists():
        return path, None

    resolved = resolve_source(source)
    sid = resolved.identify(url)
    if sid is None:
        raise ValueError(f"{url!r} does not exist as a file")

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = sorted(cache_dir.glob(f"{sid}.*"))
    cached = [p for p in cached if p.suffix not in (".part", ".ytdl")]
    if cached:
        return cached[0], sid

    tool = resolve_downloader()
    cmd = [str(tool), "--no-warnings", "-f", "bestaudio",
           "-o", str(cache_dir / f"{sid}.%(ext)s")]
    node = node_binary()
    if node is not None:
        cmd += ["--js-runtimes", f"node:{node}"]
    cmd.append(str(url))

    result = subprocess.run(cmd, capture_output=True, text=True)
    downloaded = sorted(cache_dir.glob(f"{sid}.*"))
    downloaded = [p for p in downloaded if p.suffix not in (".part", ".ytdl")]
    if result.returncode != 0 or not downloaded:
        detail = (result.stderr or result.stdout).strip().splitlines()
        tail = detail[-1] if detail else "no output"
        raise RuntimeError(f"downloader failed: {tail}")
    return downloaded[0], sid


def wav_seconds(path):
    """Length of a plain Spotykach WAV, counted from its data chunk."""
    size = Path(path).stat().st_size
    frames = max(size - PLAIN_WAV_HEADER, 0) / (4 * wb.CHANNELS)
    return frames / wb.SAMPLE_RATE


def extract_segment(src, dst, start, seconds):
    """Cut ``[start, start+seconds)`` and write a native Spotykach WAV.

    No speed is applied here: this is the plain slice, capped at the deck's
    loop length. :func:`retime_segment` is the pass that re-times it.

    Enforces the deck's loop ceiling here as well as in :func:`build_sample`, so
    a direct library call cannot produce a file the hardware would silently cut.
    """
    src, dst = Path(src), Path(dst)
    if seconds > MAX_LOOP_SECONDS + 1e-9:
        raise ValueError(
            f"segment is {seconds:g}s; a deck holds {MAX_LOOP_SECONDS:g}s max"
        )
    dst.parent.mkdir(parents=True, exist_ok=True)
    rough = dst.with_name(dst.stem + ".cut.wav")

    cmd = [
        wb.ffmpeg(), "-y", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{seconds:.3f}",
        "-i", str(src),
        "-c:a", wb.PCM_FMT, "-ar", str(wb.SAMPLE_RATE), "-ac", str(wb.CHANNELS),
        str(rough),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()}")

    try:
        # Re-header as plain IEEE float; the cut inherits ffmpeg's EXTENSIBLE
        # header, which the firmware does not read.
        wb.convert(rough, dst)
    finally:
        rough.unlink(missing_ok=True)

    # A start time past the end of the source makes ffmpeg write a header with
    # no samples. That loads on the Spotykach as silence, so refuse it.
    got = wav_seconds(dst)
    if got < seconds - max(0.25, seconds * 0.01):
        dst.unlink(missing_ok=True)
        raise RuntimeError(
            f"source is shorter than the requested range: asked for "
            f"{seconds:.2f}s from {start:.2f}s, got {got:.2f}s (empty or "
            f"truncated segment)"
        )
    return dst


def retime_video_filter(speed):
    """The ``setpts`` expression for a speed, for callers that keep video.

    ``setpts`` scales timestamps, so a clip that should last ``1/speed`` as
    long multiplies them by ``1/speed``: 0.5x (heard slow) needs 2*PTS. The
    naive ``0.5*PTS`` would halve the length and play twice as fast.
    """
    speed = validate_speed(speed)
    return f"setpts={1.0 / speed:.6g}*PTS"


def retime_segment(src, dst, speed):
    """Re-record ``src`` at ``speed``, writing the sample the deck stores.

    A second ffmpeg pass over the already-sliced segment, exactly like
    ``ffmpeg -i in -filter setpts=... out`` for a video: nothing about the cut
    changes, only how long the result lasts. ``atempo`` is the audio twin of
    ``setpts`` — ``atempo=s`` and ``setpts=PTS/s`` stretch a clip identically —
    and it is what applies here because a Spotykach sample is a bare 48 kHz
    float WAV with no video stream.

    1x copies the cut through untouched: no resample, no generation loss.
    """
    src, dst = Path(src), Path(dst)
    speed = validate_speed(speed)
    dst.parent.mkdir(parents=True, exist_ok=True)

    if abs(speed - 1.0) < 1e-9:
        if src.resolve() != dst.resolve():
            shutil.copyfile(src, dst)
        return dst

    seconds = wav_seconds(src)
    plays = seconds / speed
    if plays > MAX_LOOP_SECONDS + 1e-9:
        raise ValueError(
            f"{seconds:g}s at {speed:g}x plays for {plays:.2f}s; a deck holds "
            f"{MAX_LOOP_SECONDS:g}s max — shorten the range or raise the speed"
        )

    rough = dst.with_name(dst.stem + ".retime.wav")
    cmd = [
        wb.ffmpeg(), "-y", "-loglevel", "error",
        "-i", str(src),
        "-af", ",".join(atempo_chain(speed)),
        "-c:a", wb.PCM_FMT, "-ar", str(wb.SAMPLE_RATE), "-ac", str(wb.CHANNELS),
        str(rough),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()}")

    try:
        wb.convert(rough, dst)
    finally:
        rough.unlink(missing_ok=True)

    got = wav_seconds(dst)
    if got < plays - max(0.25, plays * 0.01):
        dst.unlink(missing_ok=True)
        raise RuntimeError(
            f"re-timed {seconds:.2f}s at {speed:g}x, expected {plays:.2f}s, "
            f"got {got:.2f}s"
        )
    return dst


# --- status --------------------------------------------------------------


def status_report(card=None, search_roots=None):
    """What can take a sample right now — the single source of truth for the GUI.

    ``ready`` is what the widget colours: False means neither transport is
    usable, which is the red state.
    """
    report = {
        "usb": {"present": False, "detail": ""},
        "card": {"present": False, "root": None, "writable": False, "detail": ""},
        "transport": None,
        "ready": False,
    }

    try:
        port = sk_install.sk_usb.find_serial_port(wait=0.0)
        midi = sk_install.sk_usb.find_midi_port()
        report["usb"]["present"] = bool(port or midi)
        report["usb"]["detail"] = (
            "in transfer mode" if port else
            "connected, will switch to transfer mode" if midi else
            "not connected"
        )
    except Exception as error:                       # noqa: BLE001 — status never raises
        report["usb"]["detail"] = str(error)

    root = Path(card) if card else None
    if root is None:
        try:
            root = autodetect_card(search_roots=search_roots)
        except Exception as error:                   # noqa: BLE001
            report["card"]["detail"] = str(error)
            root = None

    if root is not None and (root / "SK").is_dir():
        info = card_status(root)
        report["card"].update({
            "present": True,
            "root": str(root),
            "writable": info.writable,
            "detail": "mounted" if info.writable else "mounted read-only",
        })
    elif root is not None:
        report["card"]["detail"] = f"{root} has no SK/ folder"
    else:
        report["card"]["detail"] = "no card mounted"

    if report["usb"]["present"]:
        report["transport"] = "usb"
    elif report["card"]["present"] and report["card"]["writable"]:
        report["transport"] = "card"
    report["ready"] = report["transport"] is not None
    return report


def _plausible(transport, card_root):
    """Is there anything to deliver to? Presence only — never switches mode."""
    preferred = (transport or "auto").lower()
    if preferred in ("auto", "usb"):
        if (sk_install.sk_usb.find_serial_port(wait=0.0)
                or sk_install.sk_usb.find_midi_port()):
            return True
        if preferred == "usb":
            return False
    if preferred in ("auto", "card") and card_root is not None:
        return (Path(card_root) / "SK").is_dir()
    return False


def deliver(transport, card_root, tape, data, archive_dir, split=True, attempts=2):
    """Open a transport and put ``data`` on ``tape``, opening it late.

    The unit leaves transfer mode on its own after a few seconds of silence, so
    the transport is opened here, immediately before the write, and never held
    across a download and an ffmpeg pass. A drop before anything reached the
    tape is retried once: the tape is still untouched, so starting over is
    safe. A drop after a write is not retried — the shift is not idempotent.
    """
    last = None
    for attempt in range(attempts):
        try:
            chosen = sk_install.open_transport(
                transport or "auto", card_root=card_root, archive_dir=archive_dir)
        except sk_install.NoTransport as error:
            raise TransportMissing(
                "cannot deliver the sample — " + "; ".join(error.reasons)) from None
        try:
            return sk_install.install_sample(
                chosen, tape, data, archive_dir=archive_dir, shift=split)
        except sk_install.sk_usb.TransferDropped as error:
            last = error
            if getattr(chosen, "wrote", False) or attempt + 1 >= attempts:
                raise
            time.sleep(0.5)
        finally:
            chosen.close()
    raise last


def build_sample(url, start, end, is_deck_a, card=None, cache_dir=None,
                 search_roots=None, install=True, source=None, transport=None,
                 tape=None, archive_dir=None, speed=1.0):
    """Build a segment and (by default) install it on the instrument.

    ``start``/``end`` accept ``SS``/``MM:SS``/``HH:MM:SS``. When ``url`` is an
    existing local file the download step is skipped, which is what the tests
    use and what makes offline use possible.

    ``speed`` is the playback rate the range was heard at (0.25x-2x, 1x
    default). It is applied as a second pass over the sliced segment, so the
    deck receives an ordinary normal-speed WAV — just a longer or shorter one.

    Which transport will take the sample is checked before anything is
    downloaded, but the transport itself is opened only once the slice exists:
    the unit leaves transfer mode on its own after a few seconds of silence.
    USB first, the card second, unless ``transport`` names one.
    """
    start_s = parse_time(start)
    end_s = parse_time(end)
    if end_s <= start_s:
        raise ValueError(f"end ({end_s:g}s) must be after start ({start_s:g}s)")
    duration = end_s - start_s
    speed = validate_speed(speed)
    if duration > MAX_LOOP_SECONDS:
        raise ValueError(
            f"segment is {duration:g}s; a deck holds {MAX_LOOP_SECONDS:g}s max"
        )
    plays = duration / speed
    if plays > MAX_LOOP_SECONDS + 1e-9:
        raise ValueError(
            f"{duration:g}s at {speed:g}x plays for {plays:.2f}s; a deck holds "
            f"{MAX_LOOP_SECONDS:g}s max — shorten the range or raise the speed"
        )

    target_tape = (tape or DEFAULT_TAPE).upper()
    if target_tape not in wb.TAPES:
        raise ValueError(
            f"tape {target_tape!r} is not one of {' '.join(sorted(wb.TAPES))}"
        )
    cache = Path(cache_dir) if cache_dir else DEFAULT_CACHE
    archive = archive_dir or archive_target(card, cache)

    card_root = None
    if install:
        if card is not None or (transport or "auto").lower() in ("auto", "card"):
            try:
                card_root = require_card(card, search_roots)
            except CardNotFound:
                card_root = None
                if (transport or "auto").lower() == "card":
                    raise
        # Nothing plugged in should be said now, not after the download. Only
        # presence is checked here: opening the transport means switching the
        # unit into transfer mode, and it does not stay there while we work.
        if not _plausible(transport, card_root):
            try:
                sk_install.open_transport(
                    transport or "auto", card_root=card_root, archive_dir=archive
                ).close()
            except sk_install.NoTransport as error:
                raise TransportMissing(
                    "cannot deliver the sample — " + "; ".join(error.reasons)
                ) from None

    media, sid = ensure_source(url, cache, source=source)
    label = sid or Path(media).stem
    rate_tag = "" if abs(speed - 1.0) < 1e-9 else f"_{speed:g}x"
    segment = cache / "segments" / f"{label}_{int(start_s)}-{int(end_s)}{rate_tag}.WAV"
    # Slice, then re-record: the cut is always exactly the picked range, and
    # the speed only decides how long the deck's copy of it lasts.
    cut = segment.with_name(segment.stem + ".cut.WAV")
    try:
        extract_segment(media, cut, start_s, duration)
        retime_segment(cut, segment, speed)
    finally:
        cut.unlink(missing_ok=True)

    result = SampleResult(
        source=str(url), source_id=sid, start=start_s, end=end_s,
        deck="A" if is_deck_a else "B", tape=target_tape, slot=1,
        segment_path=segment, installed=False, speed=speed,
    )
    if not install:
        return result

    placement = deliver(
        transport, card_root, target_tape, segment.read_bytes(), archive,
    )
    result.installed = True
    result.transport = placement.transport
    result.shifted = placement.shifted
    result.archived = placement.archived
    if placement.transport == "card":
        result.card_path = sk_install.CardTransport(card_root).slot_path(
            target_tape, placement.slot)
    return result


# --- CLI -----------------------------------------------------------------

def format_card_status(status):
    """Human-readable card readiness block."""
    free_mb = status.free_bytes / (1024 * 1024)
    problems = structure_problems(status)
    lines = [f"card: {status.root}"]
    lines.append(f"  SK/ folder: {'yes' if status.sk.is_dir() else 'MISSING'}")
    if status.sk.is_dir():
        tapes = " ".join(status.tapes_present) or "none"
        if status.tapes_missing:
            tapes += f"   (missing: {' '.join(status.tapes_missing)})"
        lines.append(f"  tapes:      {tapes}")
    lines.append(f"  writable:   {'yes' if status.writable else 'no'}")
    lines.append(f"  free:       {free_mb:,.1f} MB")
    for name, size in status.slots.items():
        if size is None:
            lines.append(f"  slot {name}: empty")
        else:
            seconds = max(size - PLAIN_WAV_HEADER, 0) / (4 * wb.CHANNELS) / wb.SAMPLE_RATE
            lines.append(f"  slot {name}: {size:,} bytes ({seconds:.1f}s)")
    if problems:
        lines.append("state: not ready — " + "; ".join(problems))
    else:
        lines.append("state: ready")
    return "\n".join(lines)


def format_status(report):
    """Human-readable version of :func:`status_report`."""
    lines = [f"transport: {report['transport'] or 'NONE — red in the GUI'}"]
    lines.append(f"  usb:  {'yes' if report['usb']['present'] else 'no'}"
                 f"  ({report['usb']['detail']})")
    lines.append(f"  card: {'yes' if report['card']['present'] else 'no'}"
                 f"  ({report['card']['detail']})")
    return "\n".join(lines)


def _check_card(args):
    """`--check-card`: report readiness without downloading or writing."""
    root = require_card(args.card, None)
    status = card_status(root)
    print(format_card_status(status))
    problems = structure_problems(status)
    if problems:
        print(f"error: {root} is not usable: {'; '.join(problems)}. "
              "Run wav_builder.py build-card --card-root <mount> to lay out a "
              "fresh SK/ tree, or point --card at the real card.", file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="sample_tool.py",
        description=(
            "Cut a segment of any supported URL into a Spotykach slot "
            "(USB first, SD card as fallback)."
        ),
    )
    parser.add_argument("--url", "--source-url", dest="url",
                        help="URL the downloader supports, or a local file")
    parser.add_argument("--source", default=None,
                        help=f"how to identify the URL ({', '.join(sorted(SOURCES))}; "
                             f"default {DEFAULT_SOURCE})")
    parser.add_argument("--start", help="SS, MM:SS or HH:MM:SS")
    parser.add_argument("--end", help="SS, MM:SS or HH:MM:SS")
    parser.add_argument("--deck", choices=["a", "b"],
                        help="which deck loads it (isDeckA true/false)")
    parser.add_argument("--tape", default=DEFAULT_TAPE,
                        help=f"tape to write ({' '.join(sorted(wb.TAPES))}; "
                             f"default {DEFAULT_TAPE})")
    parser.add_argument("--transport", choices=["auto", "usb", "card"], default="auto",
                        help="delivery path (default auto: usb, then card)")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="playback rate the range was heard at "
                             f"({MIN_SPEED:g}-{MAX_SPEED:g}; default 1.0)")
    parser.add_argument("--card", help="card mount point (auto-detected if omitted)")
    parser.add_argument("--cache-dir", help=f"download cache (default {DEFAULT_CACHE})")
    parser.add_argument("--archive-dir", help="where samples that fall off a tape go")
    parser.add_argument("--check-card", action="store_true",
                        help="only report whether the SD card is ready, then exit")
    parser.add_argument("--status", action="store_true",
                        help="report which transports are usable right now, then exit")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if args.status:
        report = status_report(card=args.card)
        print(json.dumps(report, indent=2) if args.json else format_status(report))
        return 0 if report["ready"] else 1

    if args.check_card:
        try:
            return _check_card(args)
        except (ValueError, RuntimeError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

    missing = [flag for flag, value in (("--url", args.url), ("--start", args.start),
                                        ("--end", args.end), ("--deck", args.deck))
               if value is None]
    if missing:
        parser.error("the following arguments are required: " + ", ".join(missing))

    try:
        result = build_sample(
            args.url, args.start, args.end, args.deck == "a",
            card=args.card, cache_dir=args.cache_dir, source=args.source,
            transport=args.transport, tape=args.tape, archive_dir=args.archive_dir,
            speed=args.speed,
        )
    except (ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result.as_dict(), indent=2))
    else:
        print(f"deck {result.deck} | tape {result.tape} slot {result.slot}"
              f" via {result.transport}")
        print(f"segment: {result.segment_path} ({result.end - result.start:g}s"
              f"{'' if abs(result.speed - 1) < 1e-9 else f' at {result.speed:g}x'})")
        print(f"installed: yes, verified"
              f"{' (shifted ' + str(result.shifted) + ' slots)' if result.shifted else ''}")
        if result.archived:
            print(f"fell off the tape, archived: {result.archived}")
        if result.transport == "card":
            print("Power-cycle with the card in, then load the slot on that deck "
                  "once — that is what registers its pre-load.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
