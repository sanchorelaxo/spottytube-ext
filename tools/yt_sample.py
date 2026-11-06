#!/usr/bin/env python3
"""Spotykach YouTube sample tool — URL + time range -> deck slot on the SD card.

Turns any YouTube (or any yt-dlp supported) URL plus a start/end time into a
native Spotykach WAV placed in the tape/track slot that deck A or B loads from.

Why this exists: pulling a 30 s slice of a video into the Spotykach by hand is
four steps that break in known ways — ranged downloads of YouTube media return
403, the stale system yt-dlp fails on newer signature challenges, and a WAV
written with a WAVE_FORMAT_EXTENSIBLE header loads silently as nothing. Each of
those is handled here once.

Pipeline:
    1. resolve the newest usable yt-dlp (system one is often stale)
    2. download the full audio once, cached by video id, reused on later calls
    3. cut [start, end) with ffmpeg
    4. rewrite the header as plain IEEE float via wav_builder.convert
    5. install into SK/<tape>/<track>.WAV, backing up whatever was there

Deck mapping (storage is shared; a tape is a folder, not a deck):
    deck A -> SK/B/1.WAV      (blue tape, track 1)
    deck B -> SK/B/2.WAV      (blue tape, track 2)

Deck maximum loop is 42 s, so segments longer than that are refused rather than
silently truncated.

Usage:
    python yt_sample.py --url 'https://youtu.be/Rnltl0euaSw' \\
        --start 6:40 --end 7:10 --deck a

    # a local file works too — same pipeline, no download
    python yt_sample.py --url /path/track.wav --start 12:45 --end 13:15 --deck b
"""

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wav_builder as wb  # noqa: E402

# Deck ceiling from the manual: 42 s max loop per deck.
MAX_LOOP_SECONDS = 42.0

# wav_builder writes a bare 44-byte canonical WAV header, no fact/ext chunks.
PLAIN_WAV_HEADER = 44

# (tape, track) per deck. Both decks share the blue tape so the two clips sit
# side by side: slot 1 for A, slot 2 for B.
DECK_SLOTS = {True: ("B", 1), False: ("B", 2)}

DEFAULT_CACHE = Path.home() / ".hermes" / "cache" / "spotykach"

_VIDEO_ID_PATTERNS = (
    re.compile(r"(?:v=|/v/|/embed/|/shorts/|/live/)([A-Za-z0-9_-]{11})"),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
)


class CardNotFound(RuntimeError):
    """No SD card with an SK/ folder could be located."""


@dataclass
class SampleResult:
    source: str
    video_id: str | None
    start: float
    end: float
    deck: str
    tape: str
    track: int
    segment_path: Path
    card_path: Path | None
    installed: bool
    backup_path: Path | None = None

    def as_dict(self):
        return {
            "source": self.source,
            "video_id": self.video_id,
            "start": self.start,
            "end": self.end,
            "deck": self.deck,
            "tape": self.tape,
            "track": self.track,
            "segment": str(self.segment_path),
            "card_path": str(self.card_path) if self.card_path else None,
            "installed": self.installed,
            "backup": str(self.backup_path) if self.backup_path else None,
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


def video_id(url):
    """Return the 11-char YouTube id, or None if ``url`` is not a video URL."""
    text = str(url)
    if Path(text).exists():
        return None
    for pattern in _VIDEO_ID_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def _version_tuple(text):
    parts = re.findall(r"\d+", text)[:4]
    return tuple(int(p) for p in parts) if parts else ()


def resolve_ytdlp(candidates=None):
    """Pick the newest working yt-dlp.

    The distro copy is routinely months behind and fails on current YouTube
    signature challenges with an HTTP 403 on the media request, so version
    order decides, not PATH order.
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
    """A JS runtime for yt-dlp's signature solving, or None."""
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
    import json
    devices = []
    try:
        tree = json.loads(out.stdout)
    except ValueError:
        return []
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

def ensure_source(url, cache_dir):
    """Return ``(local_path, video_id)``; downloads once and reuses the cache."""
    path = Path(str(url))
    if path.exists():
        return path, None

    vid = video_id(url)
    if vid is None:
        raise ValueError(
            f"{url!r} is neither an existing file nor a recognizable YouTube URL"
        )

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = sorted(cache_dir.glob(f"{vid}.*"))
    cached = [p for p in cached if p.suffix not in (".part", ".ytdl")]
    if cached:
        return cached[0], vid

    tool = resolve_ytdlp()
    cmd = [str(tool), "--no-warnings", "-f", "bestaudio",
           "-o", str(cache_dir / f"{vid}.%(ext)s")]
    node = node_binary()
    if node is not None:
        cmd += ["--js-runtimes", f"node:{node}"]
    cmd.append(str(url))

    result = subprocess.run(cmd, capture_output=True, text=True)
    downloaded = sorted(cache_dir.glob(f"{vid}.*"))
    downloaded = [p for p in downloaded if p.suffix not in (".part", ".ytdl")]
    if result.returncode != 0 or not downloaded:
        detail = (result.stderr or result.stdout).strip().splitlines()
        tail = detail[-1] if detail else "no output"
        raise RuntimeError(f"yt-dlp failed: {tail}")
    return downloaded[0], vid


def extract_segment(src, dst, start, seconds):
    """Cut ``[start, start+seconds)`` and write a native Spotykach WAV.

    Enforces the deck's loop ceiling here as well as in :func:`build_sample`, so
    a direct library call cannot produce a file the hardware would silently cut.
    """
    src, dst = Path(src), Path(dst)
    if seconds > MAX_LOOP_SECONDS:
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
    size = dst.stat().st_size
    frame_bytes = 4 * wb.CHANNELS
    frames = max(size - PLAIN_WAV_HEADER, 0) / frame_bytes
    got = frames / wb.SAMPLE_RATE
    if got < seconds - max(0.25, seconds * 0.01):
        dst.unlink(missing_ok=True)
        raise RuntimeError(
            f"source is shorter than the requested range: asked for "
            f"{seconds:.2f}s from {start:.2f}s, got {got:.2f}s (empty or "
            f"truncated segment)"
        )
    return dst


def _install(segment, card_root, tape, track, backup=True, backup_dir=None):
    """Copy the segment into its slot, backing up an existing different file."""
    card_root = Path(card_root)
    sk = card_root / "SK"
    if not sk.is_dir():
        sk = wb.build_card_root(card_root)
    for letter in wb.TAPES:
        (sk / letter).mkdir(parents=True, exist_ok=True)

    dst = sk / tape / f"{track}.WAV"
    backup_path = None
    if dst.exists() and sha256(dst) != sha256(segment) and backup:
        target_dir = Path(backup_dir) if backup_dir else dst.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{tape}_{track}_previous"
        backup_path = target_dir / f"{stem}.WAV"
        counter = 1
        while backup_path.exists():
            backup_path = target_dir / f"{stem}_{counter}.WAV"
            counter += 1
        shutil.copy2(dst, backup_path)

    shutil.copy2(segment, dst)
    return dst, backup_path


def build_sample(url, start, end, is_deck_a, card=None, cache_dir=None,
                 search_roots=None, install=True, backup=True):
    """Build a deck segment and (by default) install it into its SD slot.

    ``Start``/``end`` accept ``SS``/``MM:SS``/``HH:MM:SS``. When ``url`` is an
    existing local file the download step is skipped, which is what the tests
    use and what makes offline use possible.
    """
    start_s = parse_time(start)
    end_s = parse_time(end)
    if end_s <= start_s:
        raise ValueError(
            f"end ({end_s:g}s) must be after start ({start_s:g}s)"
        )
    duration = end_s - start_s
    if duration > MAX_LOOP_SECONDS:
        raise ValueError(
            f"segment is {duration:g}s; a deck holds {MAX_LOOP_SECONDS:g}s max"
        )

    tape, track = deck_slot(is_deck_a)
    cache = Path(cache_dir) if cache_dir else DEFAULT_CACHE

    # Card first. A missing card must cost nothing, so nothing is downloaded,
    # cut or cached until a writable root is in hand.
    card_root = require_card(card, search_roots) if install else None

    source, vid = ensure_source(url, cache)
    label = vid or Path(source).stem
    segment = cache / "segments" / f"{label}_{int(start_s)}-{int(end_s)}.WAV"
    extract_segment(source, segment, start_s, duration)

    result = SampleResult(
        source=str(url), video_id=vid, start=start_s, end=end_s,
        deck="A" if is_deck_a else "B", tape=tape, track=track,
        segment_path=segment, card_path=None, installed=False,
    )
    if not install:
        return result

    dst, backup_path = _install(segment, card_root, tape, track,
                                backup=backup, backup_dir=cache / "backup")
    if sha256(dst) != sha256(segment):
        raise RuntimeError(f"copy verification failed for {dst}")

    result.card_path = dst
    result.installed = True
    result.backup_path = backup_path
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
        prog="yt_sample.py",
        description=(
            "Cut a YouTube video segment into a Spotykach deck slot "
            "(deck A -> SK/B/1.WAV, deck B -> SK/B/2.WAV)."
        ),
    )
    parser.add_argument("--url", help="YouTube URL, or a local file to skip downloading")
    parser.add_argument("--start", help="SS, MM:SS or HH:MM:SS")
    parser.add_argument("--end", help="SS, MM:SS or HH:MM:SS")
    parser.add_argument("--deck", choices=["a", "b"],
                        help="which deck loads it (isDeckA true/false)")
    parser.add_argument("--card", help="card mount point (auto-detected if omitted)")
    parser.add_argument("--cache-dir", help=f"download cache (default {DEFAULT_CACHE})")
    parser.add_argument("--check-card", action="store_true",
                        help="only report whether the SD card is ready, then exit")
    parser.add_argument("--no-backup", action="store_true",
                        help="overwrite the slot without keeping the old file")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

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
        card_root = require_card(args.card, None)
        status = card_status(card_root)
        problems = blocking_problems(status)
        if problems:
            raise CardNotFound(f"{card_root} is not usable: {'; '.join(problems)}")
        print(f"card: {card_root}  (ready)")
        if status.tapes_missing:
            print(f"note: no folder for tape {' '.join(status.tapes_missing)} yet; "
                  f"it will be created before the slot is written")
        result = build_sample(
            args.url, args.start, args.end, args.deck == "a",
            card=card_root, cache_dir=args.cache_dir, backup=not args.no_backup,
        )
    except (ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.json:
        import json
        print(json.dumps(result.as_dict(), indent=2))
    else:
        print(f"deck {result.deck} -> {result.tape}/{result.track}.WAV")
        print(f"segment: {result.segment_path} "
              f"({result.end - result.start:g}s)")
        print(f"installed: {result.card_path}")
        if result.backup_path:
            print(f"previous slot backed up: {result.backup_path}")
        print("Power-cycle with the card in, then load the slot on that deck "
              "once — that is what registers its pre-load.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
