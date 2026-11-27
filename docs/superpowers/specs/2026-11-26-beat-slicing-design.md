# Beat-aware sample slicing for SpottyTube

Date: 2026-11-26
Status: awaiting approval
Topic: a new post-processing pass that detects beats in a finished sample with
aubio and trims it to those beats, so a loop is always loop-ready. Ships first
as one checkbox with two dependent settings in the collapsed Advanced section.

## Goal

Today the sample is exactly the picked time range, retimed, level-corrected and
faded. If the pick landed mid-beat, the loop clicks or drifts out of time and
the user has to re-pick. This change adds an optional final pass: run beat
detection (aubio) over the finished segment, find the first and last beat, and
re-slice the segment to those boundaries. The result loops in time.

Success is: with the feature on, a source with a clear pulse yields a sample
whose start and end land on beats; a source with no usable pulse behaves the
way the user configured (three options below), never silently installs a
sliver, and never introduces the WAVE_FORMAT_EXTENSIBLE header bug the tool
already fixes elsewhere.

## Non-goals

- No change to the 42 s deck ceiling, transport, tape/slot, or download steps.
- No MIDI chopping, no per-beat segmentation, no time-stretching to a target
  BPM (that is shuriken's job, not SpottyTube's). One sample in, one sample out.
- No auto-detection beyond what aubio already does. These are manual knobs.
- No new Python dependency. Stdlib-only stays intact.

## Approach

aubio does detection only; ffmpeg does the cut; wav_builder rewrites the
header. aubio is shelled out as a subprocess and never writes a WAV byte.

Why this over the alternatives:

- A (picked): subprocess aubio detects beats, ffmpeg re-slices with -ss/-to,
  then the existing wav_builder header rewrite runs. Keeps stdlib-only, keeps
  aubio out of the audio bytes. Cost: a system binary on PATH and parsing its
  stdout, both patterns the tool already lives with (it drives ffmpeg and other
  subprocesses today).
- B (rejected): import aubio (the aubio-ledfx fork wheels) and call tempo()
  in-process. Cleaner code, but a real Python dependency that breaks the
  test_stdlib_only.py gate and adds an install story the project deliberately
  does not have.
- C (rejected): let aubiocut do detection and cutting (-b -c). aubiocut writes
  slices with its own sink header, reintroducing the WAVE_FORMAT_EXTENSIBLE
  header bug the tool already works around.

Compatibility note from research: aubio reads WAV through its built-in reader
and does not need ffmpeg, so there is no ffmpeg/library overlap to reconcile.
Mainline aubio on PyPI is stale (no Python 3.11+ wheels), which is another
reason the subprocess path is preferred over an in-process import.

## What was decided

### Settings (four fields, all `"section": "advanced"`, `"scope": "tool"`)

1. `slice_to_beat` — checkbox, default off. Label "Slice to auto-detected BPM
   (if any)". The master switch; every field below is ignored when it is off.

2. `slice_no_bpm` — select, default "warn". Only meaningful when slice_to_beat
   is on. Three choices:
   - `skip` — leave the sample uncut, no message.
   - `warn` — leave it uncut, surface "No BPM found, cannot reslice".
   - `block` — do not install the sample; surface the warning and leave the
     build incomplete.

3. `slice_boundary` — select, default "hard". Choices:
   - `hard` — cut exactly at the first beat and the last beat. The first beat's
     attack is the first sample.
   - `padded` — keep `slice_pad_ms` of lead-in before the first beat and a
     matching tail after the last, so the transient is not clipped.

4. `slice_pad_ms` — number, 1..200, step 1, default 5. Only used when
   slice_boundary is "padded" (same dependent-field pattern as nr_model).

### Safety floors (hardcoded, not settings)

- Fewer than 4 detected beats = "no BPM found"; the sample routes through
  slice_no_bpm unchanged.
- A trim that would leave under 0.5 s (first beat to last beat, before padding)
  is refused and routes through slice_no_bpm instead, so a 200 ms sliver is
  never installed.

### Order in the pipeline

Detection runs last, after retime and the final filters, on the finished
segment. A speed change shifts beat positions, so detecting earlier would
slice the wrong audio.

### Delivery

- aubio is invoked as a subprocess for detection only (beat positions in
  samples). It never writes the sample.
- ffmpeg re-slices the finished segment at the chosen first/last beat (with the
  pad applied when padded), then wav_builder rewrites the header as it already
  does after every cut.

## Left to the builder

- The exact aubio invocation and parsing of its output (beat positions in
  samples via `aubio beat`, or `aubiocut` without `-c`), and the default
  detection parameters (specdiff tempo, default hop/bufsize) for the first
  increment. More parameters (onset method, threshold, etc.) stay out of scope
  until this ships.
- Where the "No BPM found, cannot reslice" text lives in the bridge and how the
  widget renders the warn/block variants without new persisted state.
- Test layout, mirroring the existing chain-builder and bridge tests.
