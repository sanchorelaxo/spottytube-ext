#!/usr/bin/env python3
"""Pure builder for the ffmpeg ``-af`` chain of the Spotykach cut pass.

These are the "advanced" audio settings — high-pass, DC removal, silence trim
and edge de-click. The module is deliberately free of ffmpeg and of the
settings loader: :func:`cut_filters` takes a settings dict and returns the
comma-joined filter expression, so the ordering contract and the flag syntax
are unit-testable without spawning a process.

The filter order is fixed and meaningful::

    highpass -> dcshift -> silenceremove -> afade

* ``highpass`` removes sub-audible rumble first (it would otherwise bias the
  DC measurement and the trim threshold).
* ``dcshift`` removes any constant offset that survived.
* ``silenceremove`` trims dead air at both ends; the tail is reached by
  reversing, trimming, and reversing back.
* ``afade`` de-clicks the cut boundary last, so it always tapers real audio
  rather than silence the trim would have removed anyway.

:func:`needs_dc_probe` tells the caller whether it must measure the source's DC
offset (an extra ffmpeg ``astats`` pass) before the chain can be built. When it
returns False the cut stays a single ffmpeg pass — the default path never grows
a second one.
"""


def needs_dc_probe(cfg):
    """True when the DC shift is on, so the offset must be measured first.

    The measurement is a separate ffmpeg pass (``astats``), so it must run only
    when a shift will actually consume it.
    """
    return bool(cfg["dc_shift"])


def _highpass(cfg):
    if cfg["highpass"] and float(cfg["highpass_hz"]) > 0:
        return f"highpass=f={float(cfg['highpass_hz']):g}"
    return None


def _dc_shift(cfg, dc_offset):
    if cfg["dc_shift"] and dc_offset:
        # Remove the measured offset by shifting by its negative.
        return f"dcshift=shift={-float(dc_offset):g}"
    return None


def _silence_trim(cfg):
    if not cfg["trim_silence"]:
        return None
    threshold = f"{float(cfg['trim_threshold_db']):g}dB"
    head = f"silenceremove=start_periods=1:detection=peak:start_threshold={threshold}"
    tail = (
        f"areverse,silenceremove=start_periods=1:detection=peak:"
        f"start_threshold={threshold},areverse"
    )
    return f"{head},{tail}"


def _edge_fades(cfg, seconds):
    if cfg["edge_fade"] == "off":
        return None
    d = max(0.0, float(cfg["edge_fade_ms"]) / 1000.0)
    if d <= 0.0:
        return None
    # Never let the fade-out start before 0: on a segment shorter than twice
    # the fade the two tapers would overlap otherwise.
    out_start = max(0.0, float(seconds) - d)
    return f"afade=t=in:st=0:d={d:g},afade=t=out:st={out_start:.3f}:d={d:g}"


def cut_filters(cfg, seconds, dc_offset=0.0):
    """The comma-joined cut-pass filters for ``cfg``, or ``""`` if none apply.

    ``seconds`` is the length of the cut — the fade-out start is measured back
    from its end. ``dc_offset`` is the measured DC offset of the source and is
    used only when ``cfg["dc_shift"]`` is on.
    """
    parts = [
        _highpass(cfg),
        _dc_shift(cfg, dc_offset),
        _silence_trim(cfg),
        _edge_fades(cfg, seconds),
    ]
    return ",".join(part for part in parts if part)
