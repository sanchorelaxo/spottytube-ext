#!/usr/bin/env python3
"""Pure builder for the ffmpeg ``-af`` chain of the Spotykach cut pass.

These are the "advanced" audio settings — high-pass, DC removal, silence trim
and edge de-click. The module is deliberately free of ffmpeg and of the
settings loader: :func:`cut_filters` takes a settings dict and returns the
comma-joined filter expression, so the ordering contract and the flag syntax
are unit-testable without spawning a process.

The filter order is fixed and meaningful::

    highpass -> dcshift -> silenceremove -> afade

* ``highpass`` removes sub-audible rumble first, so what follows — most
  visibly the ``silenceremove`` threshold — sees the high-passed signal. It
  does not touch the DC measurement: that probe runs on the raw source before
  the chain and never sees this filter.
* ``dcshift`` removes any constant offset that survived.
* ``silenceremove`` trims dead air at both ends; the tail is reached by
  reversing, trimming, and reversing back.
* ``afade`` de-clicks the cut boundary last, so it always tapers real audio
  rather than silence the trim would have removed anyway. When the trim is on,
  the stream is shortened before the tail fade, so the tail taper is applied
  inside the reverse pair (an ``afade=t=in`` on the reversed stream, which is a
  fade-out at the true end); a fade-out anchored to the requested length would
  start past the shortened end and never land.

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


def _fade_seconds(cfg):
    """The de-click length in seconds, or ``None`` when no fade applies.

    ``edge_fade == "off"`` and a zero/negative length both mean no ``afade``
    term anywhere in the chain.
    """
    if cfg["edge_fade"] == "off":
        return None
    d = max(0.0, float(cfg["edge_fade_ms"]) / 1000.0)
    return d if d > 0.0 else None


def _fade_in(cfg):
    """``afade=t=in`` from the start, or ``None``. In the reversed domain this
    is also the tail taper (a fade-out at the true end)."""
    d = _fade_seconds(cfg)
    return f"afade=t=in:st=0:d={d:g}" if d is not None else None


def _fade_out(cfg, seconds):
    """``afade=t=out`` anchored to ``seconds``, or ``None``.

    Only used with the trim off, where ``seconds`` is the real stream length.
    With the trim on the tail fade is done in the reversed domain instead, so
    it lands on the shortened stream's true end.
    """
    d = _fade_seconds(cfg)
    if d is None:
        return None
    # Never let the fade-out start before 0: on a segment shorter than twice
    # the fade the two tapers would overlap otherwise.
    out_start = max(0.0, float(seconds) - d)
    return f"afade=t=out:st={out_start:.3f}:d={d:g}"


def _trim_head(cfg):
    if not cfg["trim_silence"]:
        return None
    threshold = f"{float(cfg['trim_threshold_db']):g}dB"
    return f"silenceremove=start_periods=1:detection=peak:start_threshold={threshold}"


def _trim_tail(cfg):
    if not cfg["trim_silence"]:
        return None
    threshold = f"{float(cfg['trim_threshold_db']):g}dB"
    return (f"silenceremove=start_periods=1:detection=peak:"
            f"start_threshold={threshold}")


def cut_filters(cfg, seconds, dc_offset=0.0):
    """The comma-joined cut-pass filters for ``cfg``, or ``""`` if none apply.

    ``seconds`` is the length of the cut — with the trim off the fade-out start
    is measured back from its end. ``dc_offset`` is the measured DC offset of
    the source and is used only when ``cfg["dc_shift"]`` is on.
    """
    parts = [_highpass(cfg), _dc_shift(cfg, dc_offset)]
    head = _trim_head(cfg)
    if head:
        # Trim the head, fade the head, then one reverse pair that trims the
        # tail silence and fades the tail. ffmpeg's afade rejects a negative
        # start, so once silenceremove has shortened the stream the tail fade
        # cannot be anchored to ``seconds``; doing it in the reversed domain as
        # afade=t=in lands it on the true end instead.
        parts.append(head)
        fade = _fade_in(cfg)
        if fade:
            parts.append(fade)
        parts.append("areverse")
        parts.append(_trim_tail(cfg))
        if fade:
            parts.append(fade)
        parts.append("areverse")
    else:
        parts.append(_fade_in(cfg))
        parts.append(_fade_out(cfg, seconds))
    return ",".join(part for part in parts if part)
