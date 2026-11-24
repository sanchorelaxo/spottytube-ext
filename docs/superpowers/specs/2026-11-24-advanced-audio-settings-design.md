# Advanced audio settings for SpottyTube

Date: 2026-11-24
Status: awaiting approval
Topic: 8 new ffmpeg/edge-processing settings (a ninth, mono output, was dropped),
surfaced in a collapsed "Advanced" group at the end of the Audio pipeline section
of the settings window.

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
scale; headroom is eaten by sub-bass and DC.

The change: add 8 topics of control, applied for real in the ffmpeg chain,
grouped in a new collapsible "Advanced" section that is collapsed by default.
(The original ninth, mono/stereo output, is not offered -- see D6.)

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

Eight topics, ~18 schema fields (a topic with a value and a mode needs two
controls). Defaults are chosen so that *every new setting at its default leaves
existing output byte-identical*; see Decisions D1.

| # | Topic | key(s) | control | default | range / choices |
|---|-------|--------|---------|---------|-----------------|
| 1 | Edge fades (de-click) | `edge_fade` | select | `declick` | off / declick / loop |
|   |  | `edge_fade_ms` | number | 5 | 1–200 ms |
| 2 | True-peak limiter | `true_peak` | checkbox | off | — |
|   |  | `true_peak_dbtp` | number | −1.0 | −6…0 dBTP |
| 3 | Cut accuracy | `cut_seek` | select | `fast` | fast / accurate |
| 4 | Loudnorm completion | `loudnorm_tp` | number | −1.5 | −6…0 dBTP |
|   |  | `loudnorm_lra` | number | 11 | 1–50 |
|   |  | `loudnorm_mode` | select | `dynamic` | dynamic (single-pass) / linear (two-pass) |
| 5 | Rumble + DC | `highpass` | checkbox | off | — |
|   |  | `highpass_hz` | number | 30 | 0–200 Hz |
|   |  | `dc_shift` | checkbox | off | — |
| 6 | Resample + dither | `resampler` | select | `swr` | swr / soxr |
|   |  | `dither` | select | `none` | none / triangular / triangular_hp |
| 7 | Edge silence trim | `trim_silence` | checkbox | off | — |
|   |  | `trim_threshold_db` | number | −50 | −90…−20 dB |
| 8 | Noise reduction | `noise_reduction` | checkbox | off | — |
|   |  | `nr_method` | select | `afftdn` | afftdn / arnndn |
|   |  | `nr_model` | text | "" | path to an rnnoise model (arnndn only) |

All fields carry `"scope": "tool"` — everything here is read by `sample_tool.py`
per build.

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
applied after retime so it protects the file that lands on the tape. The pass
runs only if `true_peak` is on; otherwise the retime output is already final and
nothing extra is spawned. Note `alimiter` is a sample-peak limiter;
the −1.0 dBTP default leaves margin so it behaves as a true-peak guard in
practice (documented in the setting's help text).

## Hardware results (mono probe, 2026-11-24)

Unit: Spotykach proto 1.2, firmware 1.4.0, tapes `BGPRTY`, 6 slots,
max_frames 2016000. Probe: `~/.hermes/cache/scratch/mono_probe.py` -- builds a
2 s 440 Hz tone, re-headers it through `wb.convert` at 1 and 2 channels,
uploads both through the device's own
`upload_begin`/`upload_data`/`upload_commit` frames while bypassing
`check_uploadable` (so the firmware's header check is what is tested), then
reads the slot back. The target slot was empty and was removed afterwards.

    stereo upload: OK
    mono upload:   FAILED (DeviceError: device rejected the file format)  [status 12]
    slot after:    channels=2  (the stereo control file that landed)
    readback:      byte-exact vs the stereo upload

Conclusion: the firmware accepts a 2-channel 48 kHz float WAV and refuses the
1-channel equivalent. A mono setting could only be dual mono, which saves no
space, so no such setting is offered.

## Failure modes and blast radius

1. **Mono output is not offered.** Verified on hardware: firmware 1.4.0 rejects
   a 1-channel float WAV with status 12 ("device rejected the file format")
   while the same code path accepts a 2-channel control. A "mono" setting could
   only mean dual mono (downmix, duplicate), which does not halve card usage --
   the whole point of the feature. Dropped at the user's direction; the tool
   never emits a non-stereo file. See D6.
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
  current hardcode) **except edge fade**, which defaults to `declick` at 5 ms.
  A short de-click at the cut boundary is the intended default: it is the
  biggest audible win and is inaudible on material that did not click.
  Everything else stays off. (Decided 2026-11-24.)
- **D2 Section.** New key `advanced`, label "Advanced", placed in `SECTIONS`
  right after `audio`, collapsible and collapsed. `SECTIONS` stays 2-tuples;
  metadata lives in `SECTION_META`; the bridge serves objects.
- **D3 Chain order** as written above.
- **D4 Passes are conditional.** No new ffmpeg process when nothing needs it.
- **D5 Phased rollout**, one phase at a time (plan below).
- **D6 No mono output** (dropped 2026-11-24). The firmware refuses 1-channel
  files and dual mono saves no card space, so there is no setting for it.

## Resolved on hardware (2026-11-24)

- **O1 -- mono: the firmware refuses it, so mono is dropped.** Tested against a
  connected Spotykach (proto 1.2, firmware 1.4.0, tapes `BGPRTY`, 6 slots). A
  1-channel 48 kHz float WAV uploaded through the device's own protocol is
  rejected with status 12, "device rejected the file format"; the identical
  path accepts the 2-channel control file. Dual mono would not halve card
  usage, so no such setting is offered. See D6.
- **O2 -- DC removal: measure-then-shift.** One `astats` probe pass reads the
  mean offset, then `dcshift=shift=-<dc>`. Chosen over a fixed low highpass.
- **O3 -- edge fade defaults ON** (`declick`, 5 ms). See D1.

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
- **Phase 4 — Resample and dither.** Wire the `aresample` options.
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
