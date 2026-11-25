#!/usr/bin/env python3
"""Pure builder for the ffmpeg ``-af`` chain of the Spotykach cut pass.

These are the "advanced" audio settings — high-pass, DC removal, silence trim
and edge de-click. The module is deliberately free of ffmpeg and of the
settings loader: :func:`cut_filters` takes a settings dict and returns the
comma-joined filter expression, so the ordering contract and the flag syntax
are unit-testable without spawning a process.

The filter order is fixed and meaningful::

    highpass -> dcshift -> <NR> -> silenceremove -> afade -> loudnorm -> aresample

* ``highpass`` removes sub-audible rumble first, so what follows — most
  visibly the ``silenceremove`` threshold — sees the high-passed signal. It
  does not touch the DC measurement: that probe runs on the raw source before
  the chain and never sees this filter.
* ``dcshift`` removes any constant offset that survived.
* ``<NR>`` (noise reduction) is optional and sits right after the DC shift and
  before the silence trim, so the trim threshold sees a denoised signal. It is
  off by default and adds no term; when on it is bare ``afftdn`` or
  ``arnndn=model=<nr_model>`` (see :func:`_noise_reduction`).
* ``silenceremove`` trims dead air at both ends; the tail is reached by
  reversing, trimming, and reversing back.
* ``afade`` de-clicks the cut boundary last, so it always tapers real audio
  rather than silence the trim would have removed anyway. When the trim is on,
  the stream is shortened before the tail fade, so the tail taper is applied
  inside the reverse pair (an ``afade=t=in`` on the reversed stream, which is a
  fade-out at the true end); a fade-out anchored to the requested length would
  start past the shortened end and never land.
* ``loudnorm`` closes the chain, so it measures and corrects the audio the
  fades have already shaped.
* ``aresample`` is last: ``resampler=soxr`` when the resampler is not swr and
  ``dither_method=<dither>`` when the dither is not none, merged into one
  filter (``resampler`` first, then ``dither_method``). The term appears only
  when at least one option applies, so the default chain carries no
  ``aresample`` and stays byte-identical to the legacy cut (the ``-ar 48000``
  output constraint makes this node perform the conversion; no rate argument
  is needed, and adding one is byte-identical to leaving it off).

  **Dither is inert on this tool's output.** The cut always writes
  ``pcm_f32le`` (48 kHz float stereo), and dither only bites when the bit
  depth drops; on a float32 output ``dither_method=triangular`` is
  byte-identical to no dither, with and without soxr. The term is kept for
  completeness and the setting's help text says as much — a green test here
  proves the chain term, never that dither is audible.

The de-click stays click-free even where the two tapers overlap: on a cut
shorter than twice the fade, ``afade=t=out`` is anchored at 0 so the in- and
out-tapers multiply instead of crossing, and the product is still 0 at both
ends (a 4 ms tone under a 50 ms fade measures a 0.0008 mid-point). The
behaviour is intentional and unchanged; this note only records it.

:func:`needs_dc_probe` tells the caller whether it must measure the source's DC
offset (an extra ffmpeg ``astats`` pass) before the chain can be built. When it
returns False the cut stays a single ffmpeg pass — the default path never grows
a second one.

:func:`needs_loudnorm_probe` is the loudnorm twin: ``loudnorm_mode == "linear"``
is a real measured two-pass, so it needs the source's loudness (an extra ffmpeg
pass reading the pass-1 JSON) before the chain can be built. ``"dynamic"``
needs no probe.

:func:`final_filters` is the one builder that runs *outside* the cut chain. It
is applied by ``sample_tool.build_sample`` as its own pass, after
``retime_segment`` returns, so the true-peak limiter protects the file the deck
stores at every speed — including 1x, where ``retime_segment`` only copies the
cut and spawns no ffmpeg at all. Bolt it onto the re-time pass and it would
silently do nothing in the common case.
"""


class NoiseModelMissing(ValueError):
    """Raised when ``arnndn`` is selected without a model to run it on.

    ``arnndn`` needs a ``.rnnn`` rnnoise model; with none configured the chain
    cannot be built, so building refuses here rather than emitting a filter
    that would fail deep inside ffmpeg. The check is on the setting alone — the
    path itself is never opened (see :func:`_noise_reduction`).
    """


def needs_dc_probe(cfg):
    """True when the DC shift is on, so the offset must be measured first.

    The measurement is a separate ffmpeg pass (``astats``), so it must run only
    when a shift will actually consume it.
    """
    return bool(cfg["dc_shift"])


def needs_loudnorm_probe(cfg, loudnorm=None):
    """True when loudnorm is on *and* linear mode is selected.

    Linear mode is the measured two-pass: it costs one extra ffmpeg pass to
    measure the source before the cut that applies the correction. Dynamic mode
    computes its correction in the single cut pass and needs no probe.

    ``loudnorm`` overrides ``cfg["loudnorm"]`` so the caller can thread the
    resolved on/off (CLI precedence) through.
    """
    if loudnorm is None:
        loudnorm = cfg["loudnorm"]
    return bool(loudnorm) and str(cfg.get("loudnorm_mode", "dynamic")) == "linear"


def accurate_seek(cfg):
    """True when the cut should decode-then-seek (``cut_seek == "accurate"``).

    ``fast`` (the default) keeps ``-ss`` before ``-i``, which jumps to the
    nearest keyframe; ``accurate`` moves the range after ``-i`` so ffmpeg
    decodes from the file start and honours the in-point exactly on a
    long/compressed source. Any unknown or missing value reads as ``fast``, so
    a cfg without the key — or ``cfg=None`` — stays today's behaviour.
    """
    if not cfg:
        return False
    return str(cfg.get("cut_seek", "fast")) == "accurate"


def loudnorm_filter(cfg, loudnorm=None, loudnorm_i=None, measured=None):
    """The ``loudnorm`` term for the cut chain, or ``""`` when loudnorm is off.

    ``TP`` and ``LRA`` come from ``cfg``; ``I`` from ``loudnorm_i`` (falling
    back to ``cfg["loudnorm_i"]``); all three use ``%g``, so the defaults print
    exactly as the legacy hardcode did (``I=-16:TP=-1.5:LRA=11``). When
    ``measured`` is given — the dict a linear pass 1 produced — the five
    measured values and ``linear=true`` are appended in the order ffmpeg
    requires: ``measured_I``, ``measured_TP``, ``measured_LRA``,
    ``measured_thresh``, ``offset``.
    """
    if loudnorm is None:
        loudnorm = cfg["loudnorm"]
    if not loudnorm:
        return ""
    if loudnorm_i is None:
        loudnorm_i = cfg["loudnorm_i"]
    term = (f"loudnorm=I={float(loudnorm_i):g}"
            f":TP={float(cfg['loudnorm_tp']):g}"
            f":LRA={float(cfg['loudnorm_lra']):g}")
    if measured is not None:
        term += (f":measured_I={float(measured['measured_I']):g}"
                 f":measured_TP={float(measured['measured_TP']):g}"
                 f":measured_LRA={float(measured['measured_LRA']):g}"
                 f":measured_thresh={float(measured['measured_thresh']):g}"
                 f":offset={float(measured['offset']):g}"
                 f":linear=true")
    return term


def _highpass(cfg):
    if cfg["highpass"] and float(cfg["highpass_hz"]) > 0:
        return f"highpass=f={float(cfg['highpass_hz']):g}"
    return None


def _dc_shift(cfg, dc_offset):
    if cfg["dc_shift"] and dc_offset:
        # Remove the measured offset by shifting by its negative.
        return f"dcshift=shift={-float(dc_offset):g}"
    return None


def _noise_reduction(cfg):
    """The ``<NR>`` term, or ``None`` when noise reduction is off.

    ``afftdn`` is ffmpeg's own FFT denoiser and ships bare — the spec names it
    with no parameters, and it is the default method. ``arnndn`` is the rnnoise
    model path and needs a model file, so a blank ``nr_model`` is refused here.

    This is a pure string builder: the model path is emitted verbatim and never
    stat'ed or opened, so a missing or unreadable file surfaces as an ffmpeg
    error in the pass that runs it, not as a refusal to build the chain.
    """
    if not cfg["noise_reduction"]:
        return None
    if str(cfg.get("nr_method", "afftdn")) == "arnndn":
        model = str(cfg.get("nr_model", "") or "").strip()
        if not model:
            raise NoiseModelMissing(
                "noise reduction is set to arnndn but no rnnoise model is "
                "configured — set the rnnoise model path, or use afftdn"
            )
        return f"arnndn=model={model}"
    return "afftdn"


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


def _silence_trim(cfg):
    """The ``silenceremove`` head-trim term, or ``None``.

    One helper serves both ends: the tail is trimmed by the same
    ``start_periods=1`` term applied inside the reverse pair, so head and tail
    build the identical string and only the surrounding ``areverse`` differs.
    """
    if not cfg["trim_silence"]:
        return None
    threshold = f"{float(cfg['trim_threshold_db']):g}dB"
    return (f"silenceremove=start_periods=1:detection=peak:"
            f"start_threshold={threshold}")


def resample_filter(cfg):
    """The single ``aresample`` term for the cut chain, or ``""`` (R-T4-1/2).

    Both resample options share one filter, ``resampler`` first then
    ``dither_method``:

    * ``resampler=soxr`` when the resampler is anything but ``swr``. The
      spec's exact form carries no rate argument: the cut pass already pins
      ``-ar 48000`` on the output, so this graph node performs the
      conversion, and ``aresample=resampler=soxr`` has been measured to
      differ from the default ``swr`` result on a 44.1 kHz source (see
      ``tests/test_sample_tool.py``).
    * ``dither_method=<dither>`` when the dither is not ``none``. The value
      is passed through verbatim, so a bad name fails loudly inside ffmpeg
      rather than being silently dropped.

    With the defaults (``swr`` + ``none``) this returns ``""`` and the cut
    chain gains no ``aresample`` at all, which is what keeps the default
    output byte-identical to the legacy path.

    **The dither term is inert here.** This tool always writes float32 PCM,
    and dither only changes the samples when the bit depth drops, so on the
    shipped 48 kHz float stereo output the term is byte-for-byte a no-op. The
    term is real; the audible effect is not, and no test claims one.
    """
    options = []
    if str(cfg.get("resampler", "swr")) != "swr":
        options.append("resampler=soxr")
    if str(cfg.get("dither", "none")) != "none":
        options.append(f"dither_method={cfg['dither']}")
    if not options:
        return ""
    return "aresample=" + ":".join(options)


def final_filters(cfg):
    """The post-retime final-pass ``-af`` for ``cfg``, or ``None`` if none apply.

    One term only: a true-peak limiter, run by ``sample_tool.build_sample`` as
    its own ffmpeg pass *after* ``retime_segment``. It cannot live in the cut
    chain: at 1x ``retime_segment`` copies the file and spawns no ffmpeg, so a
    limiter bolted onto the re-time pass would silently do nothing in the
    common case.

    ``limit`` is the linear ceiling for ``true_peak_dbtp`` (``10 ** (dbtp /
    20)``, rounded to 6 places). ``level=off`` is load-bearing: without it
    ``alimiter`` auto-levels the result back up to full scale and the ceiling
    is gone. Returns ``None`` (not ``""``) with ``true_peak`` off, which is the
    default, so callers can test truthiness and the default build stays
    byte-identical.

    The limiter and ``loudnorm``'s own ``TP`` both cap the peak; if the two
    disagree the tighter one wins, so set them to match (the README says so).
    This builder does not reconcile them.
    """
    if not cfg["true_peak"]:
        return None
    linear = round(10 ** (float(cfg["true_peak_dbtp"]) / 20), 6)
    return f"alimiter=limit={linear:g}:level=off"


def cut_filters(cfg, seconds, dc_offset=0.0, loudnorm=None, loudnorm_i=None,
                measured=None):
    """The comma-joined cut-pass filters for ``cfg``, or ``""`` if none apply.

    ``seconds`` is the length of the cut — with the trim off the fade-out start
    is measured back from its end. ``dc_offset`` is the measured DC offset of
    the source and is used only when ``cfg["dc_shift"]`` is on.

    ``loudnorm``, ``loudnorm_i`` and ``measured`` are threaded straight to
    :func:`loudnorm_filter` (each ``None`` falls back to ``cfg``/off). The
    loudnorm term follows the fades, and :func:`resample_filter` closes the
    chain, after loudnorm. The noise-reduction term (:func:`_noise_reduction`)
    sits after ``dcshift`` and before the silence trim.
    """
    parts = [_highpass(cfg), _dc_shift(cfg, dc_offset), _noise_reduction(cfg)]
    trim = _silence_trim(cfg)
    if trim:
        # Trim the head, fade the head, then one reverse pair that trims the
        # tail silence and fades the tail. ffmpeg's afade rejects a negative
        # start, so once silenceremove has shortened the stream the tail fade
        # cannot be anchored to ``seconds``; doing it in the reversed domain as
        # afade=t=in lands it on the true end instead.
        parts.append(trim)
        fade = _fade_in(cfg)
        if fade:
            parts.append(fade)
        parts.append("areverse")
        parts.append(trim)
        if fade:
            parts.append(fade)
        parts.append("areverse")
    else:
        parts.append(_fade_in(cfg))
        parts.append(_fade_out(cfg, seconds))
    parts.append(loudnorm_filter(cfg, loudnorm, loudnorm_i, measured))
    parts.append(resample_filter(cfg))
    return ",".join(part for part in parts if part)
