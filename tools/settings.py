#!/usr/bin/env python3
"""Single source of truth for SpottyTube settings.

One JSON file (``~/.config/spottytube/settings.json``, override the location
with ``SPOTTYTUBE_SETTINGS``) holds every user-adjustable value. The bridge
serves the schema over ``GET /settings`` and validates posted values against
it; the sampling tool reads the same file on every job. Nothing here needs a
restart: settings apply to the next save, the next countdown tick, the next
config.txt write.

Scopes say who reads a key:
    tool    sample_tool.py, per build (audio pipeline, cache location)
    widget  the browser widget (countdown, speed slider, save defaults)
    card    written to SK/config.txt on a mounted card when saved
    bridge  applied to the running bridge process (tool path, USB port)

Empty text values mean "use the built-in default" (``tool_path``,
``usb_port``, ``cache_dir``).

Every field carries a ``help`` string; the GUI shows it as the on-hover
description of the setting's label. Keep them accurate when editing the
schema — they are the user-facing documentation.
"""

import json
import math
import os
import tempfile
from pathlib import Path

# Panel order in the GUI.
SECTIONS = (
    ("audio", "Audio pipeline"),
    ("defaults", "Save defaults"),
    ("device", "Device (config.txt)"),
    ("widget", "Widget"),
    ("bridge", "Bridge and paths"),
)

_CONFIG_KEYS = ("mid_ch_a", "mid_ch_b", "mid_ps_a", "mid_ps_b",
                "pre_load", "slc_mn_a", "slc_mn_b")


def _choice(value, label):
    return {"value": value, "label": label}


SCHEMA = [
    # --- audio pipeline: what ffmpeg and the downloader do with the source ---
    {
        "key": "download_format", "section": "audio", "scope": "tool",
        "label": "Download format", "control": "select",
        "choices": [
            _choice("bestaudio", "bestaudio (downloader picks)"),
            _choice("bestaudio[ext=m4a]/bestaudio", "prefer m4a / AAC"),
            _choice("bestaudio[ext=webm]/bestaudio", "prefer webm / Opus"),
        ],
        "default": "bestaudio",
        "help": "yt-dlp format selector used when downloading the source. "
                "m4a/AAC is usually the highest-quality audio a video page "
                "offers; webm/Opus is the common alternative. Plain bestaudio "
                "lets the downloader pick. Applies to the next download.",
    },
    {
        "key": "loudnorm", "section": "audio", "scope": "tool",
        "label": "Normalize loudness", "control": "checkbox",
        "default": False,
        "help": "Run ffmpeg loudnorm over the cut so every sample lands on "
                "the tape at the same perceived volume. Costs one extra "
                "filter pass; leaves dynamics of quiet passages mostly "
                "intact at the default target.",
    },
    {
        "key": "loudnorm_i", "section": "audio", "scope": "tool",
        "label": "Loudness target (LUFS)", "control": "number",
        "min": -40.0, "max": -5.0, "step": 0.5,
        "default": -16.0,
        "help": "Integrated loudness target for loudnorm, in LUFS. Only used "
                "when Normalize loudness is on. -16 suits music; lower "
                "numbers are quieter, higher numbers are louder.",
    },
    {
        "key": "speed_method", "section": "audio", "scope": "tool",
        "label": "Speed-change method", "control": "select",
        "choices": [
            _choice("atempo", "atempo (keeps pitch)"),
            _choice("varispeed", "varispeed (pitch follows speed)"),
        ],
        "default": "atempo",
        "help": "How a speed other than 1x is baked into the sample. atempo "
                "time-stretches and keeps the original pitch; varispeed "
                "shifts pitch with speed, like slowing a tape. Both produce "
                "the same duration on the deck.",
    },

    # --- save defaults: what the widget sends with every sample ---
    {
        "key": "default_mode", "section": "defaults", "scope": "widget",
        "label": "Placement mode", "control": "select",
        "choices": [
            _choice("bump", "bump (take slot 1, shift the chain)"),
            _choice("append", "append (first free slot)"),
        ],
        "default": "append",
        "help": "Where a new sample lands. bump: slot 1 of the tape, and "
                "everything behind it moves one slot down the B G P R T Y "
                "chain; what falls off Y/6 is archived, never deleted. "
                "append: first free slot, nothing moves.",
    },
    {
        "key": "default_tape", "section": "defaults", "scope": "widget",
        "label": "Tape", "control": "select",
        "choices": [
            _choice("B", "B (blue)"), _choice("G", "G (green)"),
            _choice("P", "P (pink)"), _choice("R", "R (red)"),
            _choice("T", "T (turquoise)"), _choice("Y", "Y (yellow)"),
        ],
        "default": "B",
        "help": "Tape a new sample is written to. Tapes play in device order "
                "B G P R T Y, six slots each; in bump mode the chain shifts "
                "across tapes, in append mode the first free slot on any "
                "tape may take the sample.",
    },
    {
        "key": "default_transport", "section": "defaults", "scope": "widget",
        "label": "Transport", "control": "select",
        "choices": [
            _choice("auto", "auto (USB, then card)"),
            _choice("usb", "USB only"),
            _choice("card", "SD card only"),
        ],
        "default": "auto",
        "help": "How samples reach the instrument. auto uses USB when the "
                "unit is reachable and falls back to a mounted SD card; "
                "usb/card force one path and fail instead of falling back.",
    },

    # --- device: SK/config.txt on the card ---
    {
        "key": "mid_ch_a", "section": "device", "scope": "card",
        "label": "Deck A MIDI channel", "control": "number",
        "min": 1, "max": 16, "step": 1, "default": 1,
        "help": "MIDI channel deck A listens on (config.txt mid_ch_a, 1-16). "
                "Must differ from deck B. Written to SK/config.txt when you "
                "save with the card mounted; the unit picks it up at the next "
                "power-up.",
    },
    {
        "key": "mid_ch_b", "section": "device", "scope": "card",
        "label": "Deck B MIDI channel", "control": "number",
        "min": 1, "max": 16, "step": 1, "default": 2,
        "help": "MIDI channel deck B listens on (config.txt mid_ch_b, 1-16). "
                "Must differ from deck A. Written to SK/config.txt when you "
                "save with the card mounted; the unit picks it up at the next "
                "power-up.",
    },
    {
        "key": "mid_ps_a", "section": "device", "scope": "card",
        "label": "MIDI start/stop deck A", "control": "checkbox",
        "default": False,
        "help": "Let MIDI Start/Stop messages control deck A playback "
                "(config.txt mid_ps_a). Off means MIDI notes trigger slices "
                "but transport messages are ignored.",
    },
    {
        "key": "mid_ps_b", "section": "device", "scope": "card",
        "label": "MIDI start/stop deck B", "control": "checkbox",
        "default": False,
        "help": "Let MIDI Start/Stop messages control deck B playback "
                "(config.txt mid_ps_b). Off means MIDI notes trigger slices "
                "but transport messages are ignored.",
    },
    {
        "key": "pre_load", "section": "device", "scope": "card",
        "label": "Pre-load audio", "control": "checkbox",
        "default": True,
        "help": "Pre-load sample audio into RAM so slices trigger instantly "
                "(config.txt pre_load). Leave on unless a card is too slow "
                "and loading clicks; after changing it, power-cycle with the "
                "card in and load each slot once.",
    },
    {
        "key": "slc_mn_a", "section": "device", "scope": "card",
        "label": "Slice mono deck A", "control": "checkbox",
        "default": False,
        "help": "Deck A Slice mode: mono, no polyphony (config.txt "
                "slc_mn_a). On, a newly triggered slice cuts the previous "
                "one instead of layering over it.",
    },
    {
        "key": "slc_mn_b", "section": "device", "scope": "card",
        "label": "Slice mono deck B", "control": "checkbox",
        "default": False,
        "help": "Deck B Slice mode: mono, no polyphony (config.txt "
                "slc_mn_b). On, a newly triggered slice cuts the previous "
                "one instead of layering over it.",
    },

    # --- widget: the browser-side feel ---
    {
        "key": "danger_seconds", "section": "widget", "scope": "widget",
        "label": "Countdown danger window (s)", "control": "number",
        "min": 1, "max": 15, "step": 1, "default": 5,
        "help": "Seconds remaining on the auto-end countdown when it turns "
                "red and starts blinking. The countdown itself always spans "
                "the deck's 42 s loop ceiling.",
    },
    {
        "key": "flash_ms", "section": "widget", "scope": "widget",
        "label": "Countdown blink phase (ms)", "control": "number",
        "min": 100, "max": 2000, "step": 50, "default": 500,
        "help": "Blink phase of the red countdown in milliseconds: on for "
                "this long, off for this long. 500 is a calm pulse; lower "
                "is frantic.",
    },
    {
        "key": "speed_min", "section": "widget", "scope": "widget",
        "label": "Speed slider minimum", "control": "number",
        "min": 0.25, "max": 1.0, "step": 0.05, "default": 0.25,
        "help": "Lower bound of the widget's speed slider. The toolchain "
                "accepts 0.25x-2x, so the slider can only be narrowed, "
                "never widened past that envelope.",
    },
    {
        "key": "speed_max", "section": "widget", "scope": "widget",
        "label": "Speed slider maximum", "control": "number",
        "min": 1.0, "max": 2.0, "step": 0.05, "default": 2.0,
        "help": "Upper bound of the widget's speed slider (hardware ceiling "
                "2x). Narrow it to keep accidental fast saves off the tape.",
    },
    {
        "key": "speed_step", "section": "widget", "scope": "widget",
        "label": "Speed slider step", "control": "number",
        "min": 0.01, "max": 0.5, "step": 0.01, "default": 0.05,
        "help": "Granularity of the speed slider: each notch changes the "
                "rate by this much. Finer steps give precise rates; "
                "coarser steps cross the range faster.",
    },
    {
        "key": "speed_default", "section": "widget", "scope": "widget",
        "label": "Speed slider default", "control": "number",
        "min": 0.25, "max": 2.0, "step": 0.05, "default": 1.0,
        "help": "Speed the slider settles on for a fresh page, inside the "
                "configured minimum and maximum. Snapping to the player "
                "overrides it until snap is turned off.",
    },

    # --- bridge: runtime paths, hot-applied on save ---
    {
        "key": "tool_path", "section": "bridge", "scope": "bridge",
        "label": "Sampling tool path", "control": "text", "default": "",
        "help": "Override path to the sampling tool script (.py). Empty uses "
                "the tool bundled with this install. Applied immediately to "
                "the next job; a path that does not exist is refused.",
    },
    {
        "key": "usb_port", "section": "bridge", "scope": "bridge",
        "label": "USB serial port", "control": "text", "default": "",
        "help": "Force the serial port used for USB transfers (for example "
                "/dev/ttyACM1). Empty auto-detects the Spotykach. Applied "
                "to the next save without restarting the bridge.",
    },
    {
        "key": "cache_dir", "section": "bridge", "scope": "tool",
        "label": "Download cache directory", "control": "text", "default": "",
        "help": "Where downloads and cut segments are cached. Empty uses "
                "~/.hermes/cache/spotykach. Point it at a bigger disk if "
                "sources are large; existing cache files are not moved.",
    },
]

KEYS = tuple(field["key"] for field in SCHEMA)
FIELDS = {field["key"]: field for field in SCHEMA}
CARD_KEYS = _CONFIG_KEYS
TEXT_KEYS = ("tool_path", "usb_port", "cache_dir")

MAX_TEXT_LEN = 512


def schema_json():
    """The schema, serializable, for GET /settings."""
    return [dict(field) for field in SCHEMA]


def defaults():
    """Every key at its schema default."""
    return {field["key"]: field["default"] for field in SCHEMA}


def config_path():
    """Where settings live; SPOTTYTUBE_SETTINGS overrides (tests, odd setups)."""
    override = os.environ.get("SPOTTYTUBE_SETTINGS")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "spottytube" / "settings.json"


def _coerce(field, value):
    """Return ``(clean_value, error)`` for one field."""
    key = field["key"]
    control = field["control"]

    if control == "select":
        text = str(value)
        allowed = [choice["value"] for choice in field["choices"]]
        if text not in allowed:
            return None, f"{key} must be one of {', '.join(allowed)}"
        return text, None

    if control == "checkbox":
        if isinstance(value, bool):
            return value, None
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value), None
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "0", "1"):
            return value.strip().lower() in ("true", "1"), None
        return None, f"{key} must be true or false"

    if control == "number":
        if isinstance(value, bool):
            return None, f"{key} must be a number"
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None, f"{key} must be a number"
        if not math.isfinite(number):
            return None, f"{key} must be a finite number"
        low, high = field["min"], field["max"]
        if number < low or number > high:
            return None, f"{key} must be between {low:g} and {high:g}"
        if isinstance(field.get("step", 1), int):
            return int(round(number)), None
        return round(number, 6), None

    # text
    if not isinstance(value, str):
        return None, f"{key} must be text"
    text = value.strip()
    if len(text) > MAX_TEXT_LEN:
        return None, f"{key} is too long (max {MAX_TEXT_LEN} characters)"
    return text, None


def _cross_check(cfg):
    """Rules that span fields. ``cfg`` is complete (defaults filled in)."""
    problems = []
    if cfg["speed_default"] < cfg["speed_min"]:
        problems.append("speed_default cannot be below speed_min")
    if cfg["speed_default"] > cfg["speed_max"]:
        problems.append("speed_default cannot be above speed_max")
    if cfg["mid_ch_a"] == cfg["mid_ch_b"]:
        problems.append("mid_ch_a and mid_ch_b must differ — both decks "
                        "share one MIDI port")
    tool_path = (cfg.get("tool_path") or "").strip()
    if tool_path and not tool_path.endswith(".py"):
        problems.append("tool_path must point at a .py script")
    return problems


def load(path=None):
    """Current settings: stored values over schema defaults.

    Never raises. A missing, corrupt, or partially invalid file degrades to
    defaults per key, so a hand-edited file cannot brick the widget or the
    bridge.
    """
    cfg = defaults()
    file_path = Path(path) if path else config_path()
    try:
        raw = json.loads(file_path.read_text())
    except (OSError, ValueError):
        return cfg
    if not isinstance(raw, dict):
        return cfg
    for key, value in raw.items():
        field = FIELDS.get(key)
        if field is None:
            continue
        coerced, error = _coerce(field, value)
        if error is None:
            cfg[key] = coerced
    return cfg


def validate(values, base=None):
    """Validate posted values. Returns ``(merged_settings, errors)``.

    ``values`` may be partial: keys it does not carry keep their stored (or
    default) value. Unknown keys are an error — a stale panel should not
    silently drop what the user typed.
    """
    if not isinstance(values, dict):
        return None, ["expected a JSON object"]
    errors = []
    clean = {}
    for key, value in values.items():
        field = FIELDS.get(key)
        if field is None:
            errors.append(f"unknown setting {key!r}")
            continue
        coerced, error = _coerce(field, value)
        if error:
            errors.append(error)
        else:
            clean[key] = coerced
    merged = dict(base if base is not None else load())
    merged.update(clean)
    if not errors:
        errors.extend(_cross_check(merged))
    if errors:
        return None, errors
    return merged, []


def save(values, path=None):
    """Atomically write a complete settings dict. Returns the path."""
    file_path = Path(path) if path else config_path()
    file_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(values, indent=2, sort_keys=True) + "\n"
    handle_fd, tmp = tempfile.mkstemp(dir=str(file_path.parent),
                                      prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(handle_fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, file_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return file_path
