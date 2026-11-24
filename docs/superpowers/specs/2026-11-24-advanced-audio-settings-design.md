# Advanced audio settings for SpottyTube

Date: 2026-11-24
Status: awaiting approval
Topic: 9 new ffmpeg/edge-processing settings, surfaced in a collapsed "Advanced"
group at the end of the Audio pipeline section of the settings window.

## Goal

The audio pipeline today exposes two knobs: Normalize loudness (on/off) and the
LUFS target. Everything else is hardcoded and invisible:

- `extract_segment` builds `-af loudnorm=I=<i>:TP=-1.5:LRA=11` (single-pass,
  dynamic) — `tools/sample_tool.py:620`.
- every ffmpeg pass pins `-ac 2` — `sample_tool.py:622` and `:714`.
- the cut seeks with `-ss` before `-i` (fast input seek) — `sample_tool.py:616`.
- no de-click at the cut boundary, nothing after the retime pass, no
  high-pass/DC, no silence trim, no denoise, no resampler/dither choice.

Consequences the user named: a hard slice of a loud waveform clicks and that
click is baked into every trigger; the retime pass can push peaks past full
scale; headroom is eaten by sub-bass and DC; samplers that want mono pay for
stereo.

The change: add 9 topics of control, applied for real in the ffmpeg chain,
grouped in a new collapsible "Advanced" section that is collapsed by default.

## Non-goals

- No change to the 42 s deck ceiling, the transport choice, the tape/slot
  logic, or the download step.
- No video handling. No re-encode of samples already on a card.
- Nothing auto-detected. These are manual knobs with safe defaults.

## Approach

Schema-first, consistent with commit 925a9ca ("one schema drives panel, tool
and bridge"). All new fields live in `tools/settings.py`; the bridge serves
them; the panel renders them; `sample_tool.py` reads them per build. The only
genuinely new UI capability is the collapse, because `SECTIONS` is flat today.

### Sections and the collapse

`SECTIONS` stays a tuple of `(key, label)` 2-tuples — `tests/test_settings.py:54`
unpacks it that way and the tool/bridge depend on the shape. Collapse is added
as a separate map, not by widening the tuples:

```python
SECTIONS = (
    ("audio", "Audio pipeline"),
    ("advanced", "Advanced"),          # new — sits right after audio
    ("defaults", "Save defaults"),
    ("device", "Device (config.txt)"),
    ("widget", "Widget"),
    ("bridge", "Bridge and paths"),
)
SECTION_META = {"advanced": {"collapsible": True, "collapsed": True}}
```

The bridge currently serves `"sections": [list(s) for s in SECTIONS]`
(`bridge/spottytube_bridge.py:448`). It becomes a list of objects
`{"key", "label", "collapsible", "collapsed"}`, merging `SECTION_META`. Nothing
pins the old payload shape (`tests/test_bridge.py` has no `sections` assertion),
so this is a safe widening. `extension/content.js` `sectionOrder()` and
`renderFields()` (`content.js:643`, `:654`) read `entry.key`/`entry.label`,
with a fallback to the array form so the schema-derived path
(`[section, section]`) still works when the bridge sends no `sections`.

`renderFields()` wraps a collapsible section's rows in a `.sgroup` div, renders
the head as a clickable `.ssec` with a caret, and sets `hidden` on the group
when collapsed. Toggling flips `hidden`; open/closed lives in `settingsState`
only (ephemeral — the group re-collapses on the next open, which is what
"collapsed by default" means). CSS additions are small: `.ssec.toggle`,
`.ssec .caret`, `.sgroup[hidden]`.

### The 9 topics

Nine topics, ~19 schema fields (a topic with a value and a mode needs two
controls). Defaults are chosen so that *every new setting at its default leaves
existing output byte-identical*; see Decisions D1.

| # | Topic | key(s) | control | default | range / choices |
|---|-------|--------|---------|---------|-----------------|
| 1 | Edge fades (de-click) | `edge_fade` | select | `off` | off / declick / loop |
|   |  | `edge_fade_ms` | number | 5 | 1–200 ms |
| 2 | True-peak limiter | `true_peak` | checkbox | off | — |
|   |  | `true_peak_dbtp` | number | −1.0 | −6…0 dBTP |
| 3 | Cut accuracy | `cut_seek` | select | `fast` | fast / accurate |
| 4 | Output channels | `output_channels` | select | `stereo` | stereo / mono |
| 5 | Loudnorm completion | `loudnorm_tp` | number | −1.5 | −6…0 dBTP |
|   |  | `loudnorm_lra` | number | 11 | 1–50 |
|   |  | `loudnorm_mode` | select | `dynamic` | dynamic (single-pass) / linear (two-pass) |
| 6 | Rumble + DC | `highpass` | checkbox | off | — |
|   |  | `highpass_hz` | number | 30 | 0–200 Hz |
|   |  | `dc_shift` | checkbox | off | — |
| 7 | Resample + dither | `resampler` | select | `swr` | swr / soxr |
|   |  | `dither` | select | `none` | none / triangular / triangular_hp |
| 8 | Edge silence trim | `trim_silence` | checkbox | off | — |
|   |  | `trim_threshold_db` | number | −50 | −90…−20 dB |
| 9 | Noise reduction | `noise_reduction` | checkbox | off | — |
|   |  | `nr_method` | select | `afftdn` | afftdn / arnndn |
|   |  | `nr_model` | text | "" | path to an rnnoise model (arnndn only) |

All fields carry `"scope": "tool"` except `output_channels`, which is also
`"tool"` — everything here is read by `sample_tool.py` per build.

### Where each one lands in the chain

Cut pass (`extract_segment`), in order:

    highpass → dcshift → afftdn → silenceremove → edge fades → loudnorm → aresample

- `highpass=f=<hz>` when `highpass`.
- DC: measure mean offset once (`astats`), then `dcshift=shift=-<dc>` when
  `dc_shift`. Measure-then-shift is exact; the alternative (a 1 Hz highpass)
  is a guess. See O2.
- `afftdn` (adaptive) when `noise_reduction` and method is afftdn;
  `arnndn=model=<nr_model>` when method is arnndn (refuses with a clear message
  if `nr_model` is empty).
- `silenceremove=start_periods=1:detection=peak:start_threshold=<db>` plus the
  reversed form for the tail, when `trim_silence`.
- Edge fades, `edge_fade_ms` D: `declick` = `afade=t=in:d=D` and
  `afade=t=out:st=<seconds-D>:d=D`; `loop` = a complementary in/out pair of
  equal length so the wrap is click-free (the head fade-in and tail fade-out
  use matching equal-power curves; implemented as an `afade` pair over the
  reversed tail, or `acrossfade` between tail and head — pick at build time).
- `loudnorm=I=<i>:TP=<loudnorm_tp>:LRA=<loudnorm_lra>` — `linear=true` and a
  measured first pass when `loudnorm_mode` is linear, i.e. one extra pass.
- `aresample=resampler=<resampler>` (soxr when chosen) and
  `aresample=dither_method=<dither>` when not `none`.

Seek: `cut_seek=fast` keeps `-ss` before `-i` (today). `accurate` moves `-ss`
after `-i` (decode-then-seek) so the in-point is honored exactly on a
long/compressed source.

Retime pass: unchanged.

Final pass (new, runs only when needed):

    alimiter (true peak) and/or -ac <channels>

`alimiter=limit=<linear>:level=off` with `linear = 10**(true_peak_dbtp/20)`,
applied after retime so it protects the file that lands on the tape. `-ac 1`
when `output_channels=mono`. The pass runs only if `true_peak` is on or the
channel count differs from the source; otherwise the retime output is already
final and nothing extra is spawned. Note `alimiter` is a sample-peak limiter;
the −1.0 dBTP default leaves margin so it behaves as a true-peak guard in
practice (documented in the setting's help text).

## Failure modes and blast radius

1. **Mono breaks the duration math.** `sample_tool.wav_seconds` hardcodes
   `4 * wb.CHANNELS` (`sample_tool.py:586`, `:981`) and `wb.probe`'s slot
   validation asserts `channels == CHANNELS` (`wav_builder.py:154`). A mono
   file is 4 bytes/frame, so every duration check and the deck-ceiling guard
   go wrong. `wb.convert(src, dst, sr, ch, fmt)` already takes `ch`, so the
   plumbing is `wav_seconds(path, channels)` + a channels-aware slot check.
   **Unverified: does the firmware load a 1-channel float WAV?** Needs a
   hardware test before mono ships (O1).
2. **Trim vs the length guard.** `extract_segment` refuses a segment shorter
   than requested (`sample_tool.py:639`) as a "source shorter than range"
   error. `trim_silence` intentionally shortens the segment, so the guard must
   be relaxed when trimming is on (or the trim measured and subtracted).
3. **Pass count.** Each new pass costs wall time per sample: linear loudnorm
   +1, DC probe +1, final limiter +1. The no-op case spawns nothing, so the
   default path is unchanged.
4. **Double limiting.** If `loudnorm_tp` and `true_peak_dbtp` disagree, the
   limiter undoes loudnorm's ceiling. Help text steers the user to match them.
5. **Dither is mostly moot** at 32-bit float output; the control exists for
   completeness and is documented as such rather than oversold.
6. **`arnndn` needs a model file** with no built-in default; refuse clearly
   when `nr_model` is empty instead of failing inside ffmpeg.
7. **Collapse regression.** `sectionOrder()` must still work when the bridge
   sends no `sections` array (schema-derived `[section, section]` pairs).

## Decisions

- **D1 Defaults.** Every new setting defaults to today's behavior (off, or the
  current hardcode). No existing sample changes until a user opts in. This
  applies even to edge fades, the "biggest audible win": it is one toggle away,
  not on by default. (Confirm — see O3.)
- **D2 Section.** New key `advanced`, label "Advanced", placed in `SECTIONS`
  right after `audio`, collapsible and collapsed. `SECTIONS` stays 2-tuples;
  metadata lives in `SECTION_META`; the bridge serves objects.
- **D3 Chain order** as written above.
- **D4 Passes are conditional.** No new ffmpeg process when nothing needs it.
- **D5 Phased rollout**, one phase at a time (plan below).

## Open questions

- **O1** Does the Spotykach firmware accept a mono float WAV? Blocks the
  default for `output_channels` only after a hardware test; mono ships opt-in.
- **O2** DC removal: measure-then-shift (one probe pass) vs a fixed low
  highpass. Recommend measure-then-shift.
- **O3** Confirm D1: keep all new settings off by default, or turn the
  de-click edge fade on by default because it is the largest audible win?

## Phased implementation plan

One phase at a time, each TDD (RED-GREEN-REFACTOR), each with schema help text
and README updates, each committed on its own.

- **Phase 1 — Schema, section metadata, panel.** Add the fields and
  `SECTION_META`; widen the bridge `/settings` payload; render the collapsed
  `Advanced` group with a caret. No audio behavior change. Tests: settings
  coerce/validate for each new field; bridge payload shape; widget wiring for
  the collapse (render, default-collapsed, toggle, save round-trip).
- **Phase 2 — Cut-pass filters.** highpass, dcshift, afftdn/arnndn, silence
  trim, edge fades. Tests decode the produced WAVs: length stays right, edges
  are tapered, silence is trimmed, the length guard tolerates the trim.
- **Phase 3 — Loudnorm completion.** `loudnorm_tp`/`loudnorm_lra` replace the
  hardcodes; `loudnorm_mode=linear` adds the measured two-pass path.
- **Phase 4 — Resample, dither, channels.** Wire `aresample` options; make
  `wav_seconds` and the slot check channels-aware; add the `-ac` plumbing;
  hardware-verify mono (O1).
- **Phase 5 — Final limiter and noise reduction.** The post-retime `alimiter`
  pass and the `afftdn`/`arnndn` path, with the conditional-pass rule (D4).

## Files touched

- `tools/settings.py` — new fields, `SECTION_META`.
- `bridge/spottytube_bridge.py` — `/settings` `sections` payload.
- `extension/content.js` — collapse mechanism, CSS, `sectionOrder`.
- `tools/sample_tool.py` — `extract_segment`, `retime_segment`/new finalizer,
  `build_sample`, `wav_seconds`.
- `tests/test_settings.py`, `tests/test_widget_wiring.mjs`,
  `tests/test_sample_tool.py`, `tests/test_bridge.py`.
- `README.md` — the Advanced group and what each setting does.
