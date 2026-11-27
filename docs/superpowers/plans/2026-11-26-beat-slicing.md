# Beat-aware sample slicing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional final pass that runs aubio beat detection over the finished sample, finds the first and last beat, and re-slices the segment to those boundaries so a loop is loop-ready, with a settings select governing the no-BPM outcome.

**Architecture:** New stdlib-only module `tools/beat_slice.py` shells out to aubio for detection only (it never writes audio bytes), computes slice bounds from its stdout, and re-cuts with ffmpeg decoded to raw f32le that `wav_builder._write_plain_float_wav` rewrites as a plain IEEE-float header. `sample_tool.build_sample` calls it after retime and final filters; the no-BPM policy (skip / warn / block) lives there and rides the existing result JSON. No bridge change: warn surfaces through the existing `job.result` block, block surfaces through the existing nonzero-exit error path.

**Tech Stack:** Python 3 stdlib (tools/ and bridge/ are stdlib-only, enforced by `tests/test_stdlib_only.py`), ffmpeg (already a pipeline dependency), `aubio` CLI (optional external binary, resolved like `wav_builder.ffmpeg()`), node `--test` for the widget wiring.

**Spec:** `docs/superpowers/specs/2026-11-26-beat-slicing-design.md`

## Global Constraints

- Stdlib-only in `tools/` and `bridge/`: no third-party Python imports (test_stdlib_only.py fences it).
- Output is always plain 48 kHz float WAV; every cut must be re-headered with `wb._write_plain_float_wav` (format tag `0x0003`), never leave ffmpeg's `WAVE_FORMAT_EXTENSIBLE` header.
- Beat floor: fewer than 4 detected beats routes through `slice_no_bpm`, never a partial slice.
- Outcome floor: a trim whose first-beat-to-last-beat span is under 0.5 s routes through `slice_no_bpm`.
- The warning string is exactly `No BPM found, cannot reslice`.
- Settings are the single source of truth in `tools/settings.py`; every schema field must carry on-hover `help` and a README `## Settings` bullet in the exact parsed shape.
- New test modules are `unittest.TestCase` subclasses (discovery runs `-s tests -t .`), not bare `def test_...` functions.
- Detection runs last, after retime and final filters, on the finished `segment`.
- `aubio` may be absent from PATH; absence is a distinct, loud failure (not a "no BPM" signal).

## Review Focus

- **`aubio` missing while `slice_to_beat` is on** — must raise a clear RuntimeError naming the missing binary and the install hint, not silently skip or misroute as no-BPM (a silent pass would hide a broken opt-in).
- **aubio prints non-numeric or stray stdout** (a bpm summary line, blank lines) — the parser must tolerate and drop non-finite/non-float tokens, never crash on them.
- **`padded` boundary with a first beat near time 0** — lead-in must clamp to `[0.0, total]`, never seek before the file start (negative `-ss` would decode silence or fail).
- **Warn path still installs** — `build_sample` must return a result with `warning` set and the normal `installed: true` block, not a partial result; the widget shows both the success and the warning.
- **Block path installs nothing** — the RuntimeError must raise before transport/deliver, so the card is untouched and the job records a nonzero exit with the message on stderr.

---

### Task 1: Beat detection primitives

**Files:**
- Create: `tools/beat_slice.py`
- Create: `tests/test_beat_slice.py`

**Interfaces:**
- Consumes: nothing (stdlib + `subprocess` only).
- Produces:
  - `aubio_binary() -> str | None` — `shutil.which("aubio")`, `None` when absent.
  - `parse_beats(stdout: str) -> list[float]` — sorted unique finite floats (seconds).
  - `detect_beats(path, binary: str | None = None) -> list[float]` — raises `RuntimeError` on missing binary or nonzero exit.

- [ ] **Step 1: Load the new module and write the failing parse test**

Mirror the `tests/test_sample_tool.py` `_load()` importlib-by-path helper (it already inserts `tools/` on `sys.path`); a new `class TestParseBeats(unittest.TestCase)` unit-tests the pure parser with no subprocess.

```python
class TestParseBeats(unittest.TestCase):
    def test_parse_beats_sorts_dedupes_and_drops_junk(self):
        bs = _load("beat_slice")
        self.assertEqual(bs.parse_beats("0.5\n1.25\n0.5\n\nbpm: 120\nnot-a-number\n2.0\n"),
                         [0.5, 1.25, 2.0])

    def test_parse_beats_empty_output(self):
        bs = _load("beat_slice")
        self.assertEqual(bs.parse_beats(""), [])
```

- [ ] **Step 2: Run the parse test to verify it fails**

Run: `python3 -m unittest tests.test_beat_slice.TestParseBeats -v`
Expected: FAIL with `ModuleNotFoundError` / `_load` target missing.

- [ ] **Step 3: Implement `aubio_binary`, `parse_beats` in `tools/beat_slice.py`**

Create the module (stdlib-only; import `shutil`, `subprocess`, `Path`). `aubio_binary()` returns `shutil.which("aubio")` (no bare-name fallback, so callers can detect absence). `parse_beats` splits on whitespace/newlines, keeps tokens that `float()` cleanly (`try/except ValueError`), drops non-finite values (`math.isfinite`), returns `sorted(set(...))`.

- [ ] **Step 4: Run the parse test to verify it passes**

Run: `python3 -m unittest tests.test_beat_slice.TestParseBeats -v`
Expected: PASS.

- [ ] **Step 5: Write the failing invocation test with a fake aubio**

A `class TestDetectBeats(unittest.TestCase)` writes a tiny executable Python script to a temp file that prints fixed beat times to stdout and exits 0, then points `detect_beats(..., binary=fake_path)` at it. This tests the full subprocess + parse path without the real binary (aubio's own algorithm is upstream's contract, not ours). A second `detect_beats` test covers a nonzero-exit script raising RuntimeError, and a third asserts a missing `default_binary` raises RuntimeError containing `"aubio"`.

```python
def test_detect_beats_runs_binary_and_parses_stdout(self):
    fake = Path(tempfile.mkdtemp()) / "fake_aubio"
    fake.write_text("#!/usr/bin/env python3\nprint('0.5\\n1.0\\n1.5\\n2.0')\n")
    fake.chmod(0o755)
    self.assertEqual(detect_beats(source_wav, binary=str(fake)), [0.5, 1.0, 1.5, 2.0])
```

- [ ] **Step 6: Implement `detect_beats(path, binary=None)` in `tools/beat_slice.py`**

`binary = binary or aubio_binary()`; raise `RuntimeError("aubio is not installed; install aubio-tools (sudo apt install aubio-tools) to slice to the beat")` when `None`. Run `[binary, "beat", "-T", "seconds", str(path)]` with `capture_output=True, text=True`; on nonzero return code raise `RuntimeError(f"aubio failed: {stderr.strip()}")`; else return `parse_beats(stdout)`.

- [ ] **Step 7: Run the invocation tests to verify they pass**

Run: `python3 -m unittest tests.test_beat_slice.TestDetectBeats -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add tools/beat_slice.py tests/test_beat_slice.py
git commit -m "feat: add aubio beat detection primitives"
```

---

### Task 2: Beat bounds and re-slice

**Files:**
- Modify: `tools/beat_slice.py`
- Modify: `tests/test_beat_slice.py`

**Interfaces:**
- Consumes: `parse_beats` / `detect_beats` from Task 1; `wav_builder._write_plain_float_wav(path, pcm_float_bytes, sr, ch)` (existing).
- Produces:
  - `MIN_BEATS = 4` and `MIN_SPAN_SECONDS = 0.5` module constants.
  - `choose_bounds(beats: list[float], boundary: str, pad_ms: float, total_seconds: float) -> tuple[float, float] | None` — `(start, end)` seconds, or `None` when beats `< 4` or `beats[-1] - beats[0] < 0.5`.
  - `reslice(src: Path, dst: Path, start_s: float, end_s: float) -> Path` — ffmpeg `-ss -to` cut decoded to raw f32le, then plain-float header rewrite.

- [ ] **Step 1: Write the failing bounds test**

```python
class TestChooseBounds(unittest.TestCase):
    def test_under_four_beats_returns_none(self):
        self.assertIsNone(choose_bounds([0.0, 1.0, 2.0], "hard", 0, 10.0))

    def test_span_under_half_second_returns_none(self):
        # 4 beats but squashed into 0.4 s
        self.assertIsNone(choose_bounds([0.0, 0.1, 0.2, 0.4], "hard", 0, 10.0))

    def test_hard_cut_is_first_to_last_beat(self):
        self.assertEqual(choose_bounds([0.5, 1.0, 1.5, 2.0], "hard", 0, 10.0), (0.5, 2.0))

    def test_padded_keeps_lead_in_and_tail(self):
        self.assertEqual(choose_bounds([0.5, 1.0, 1.5, 2.0], "padded", 25, 10.0), (0.475, 2.025))

    def test_padded_clamps_to_file_bounds(self):
        self.assertEqual(choose_bounds([0.01, 0.5, 1.0, 1.5], "padded", 25, 10.0), (0.0, 1.525))
```

- [ ] **Step 2: Run the bounds test to verify it fails**

Run: `python3 -m unittest tests.test_beat_slice.TestChooseBounds -v`
Expected: FAIL with `NameError` (functions not defined).

- [ ] **Step 3: Implement `choose_bounds` in `tools/beat_slice.py`**

`pad = pad_ms / 1000.0` when boundary is `"padded"`, else `0.0`. Return `None` unless `len(beats) >= MIN_BEATS` and `beats[-1] - beats[0] >= MIN_SPAN_SECONDS` (span measured before padding, per spec). `start = max(0.0, beats[0] - pad)`, `end = min(total_seconds, beats[-1] + pad)`.

- [ ] **Step 4: Run the bounds test to verify it passes**

Run: `python3 -m unittest tests.test_beat_slice.TestChooseBounds -v`
Expected: PASS.

- [ ] **Step 5: Write the failing re-slice test (real ffmpeg)**

Reuse the existing `wav()` float fixture (48 kHz stereo) to build a source, then cut `[0.25, 0.75]` and decode the result.

```python
class TestReslice(unittest.TestCase):
    def test_reslice_writes_plain_float_wav_of_requested_span(self):
        src = wav(seconds=1.0)          # existing fixture helper
        out = src.with_suffix(".beat.WAV")
        reslice(src, out, 0.25, 0.75)
        # header fmt tag 3 (not 0xFFFE) at byte offset 20, little-endian
        header = out.read_bytes()[:44]
        self.assertEqual(int.from_bytes(header[20:22], "little"), 3)
        self.assertEqual(int.from_bytes(header[24:28], "little"), 48000)
        pcm = out.read_bytes()[44:]
        self.assertEqual(len(pcm), round(0.5 * 48000) * 4 * 2)   # 2ch * 4 bytes/sample
```

- [ ] **Step 6: Run the re-slice test to verify it fails**

Run: `python3 -m unittest tests.test_beat_slice.TestReslice -v`
Expected: FAIL with `NameError`.

- [ ] **Step 7: Implement `reslice(src, dst, start_s, end_s)` in `tools/beat_slice.py`**

Import `wav_builder as wb`. Run ffmpeg exactly as `wb.convert` plus the range: `[ffmpeg(), "-y", "-ss", f"{start_s:.6f}", "-to", f"{end_s:.6f}", "-i", str(src), "-f", "f32le", "-acodec", "pcm_f32le", "-ar", str(wb.SAMPLE_RATE), "-ac", str(wb.CHANNELS), "-"]`, `capture_output=True`. On nonzero exit raise `RuntimeError(f"ffmpeg failed: {stderr.decode(errors='replace').strip()}")`. Write with `wb._write_plain_float_wav(dst, result.stdout, wb.SAMPLE_RATE, wb.CHANNELS)` and return `Path(dst)`. This is the plain-float header rewrite, not ffmpeg's EXTENSIBLE sink — the exact bug Approach A avoids.

- [ ] **Step 8: Run the re-slice test to verify it passes**

Run: `python3 -m unittest tests.test_beat_slice.TestReslice -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add tools/beat_slice.py tests/test_beat_slice.py
git commit -m "feat: compute beat slice bounds and re-slice to plain float WAV"
```

---

### Task 3: Settings schema (four fields)

**Files:**
- Modify: `tools/settings.py` (SCHEMA, advanced section)
- Modify: `tests/test_settings.py`
- Modify: `README.md` (`## Settings` -> `### Advanced`)

**Interfaces:**
- Consumes: the existing `_choice()` helper and the `"section": "advanced"`, `"scope": "tool"` shape.
- Produces four keys read later by `sample_tool`: `slice_to_beat` (bool, default False), `slice_no_bpm` (str, default "warn"), `slice_boundary` (str, default "hard"), `slice_pad_ms` (int, default 5).

- [ ] **Step 1: Append the four fields to SCHEMA in `tools/settings.py`, in the advanced section, in this order**

1. `slice_to_beat` — `control: "checkbox"`, default `False`, label `"Slice to auto-detected BPM (if any)"`, help text naming aubio and the three outcomes.
2. `slice_no_bpm` — `control: "select"` with `_choice()` entries: `("skip", "Skip silently (sample passes through uncut, no messages)")`, `("warn", "warn user but still send ('No BPM found, cannot reslice')")`, `("block", "Do not send if no BPM found")`; default `"warn"`.
3. `slice_boundary` — `control: "select"` with `("hard", "Hard cut")` and `("padded", "Padded cut")`; default `"hard"`.
4. `slice_pad_ms` — `control: "number"`, `min: 1`, `max: 200`, `step: 1`, default `5`, help naming that it only applies when `slice_boundary` is `"padded"`.

Every field gets a non-empty `help` string (the hover doc). Follow the `nr_model` dependent-field shape for `slice_pad_ms` and keep the help prose word-parallel with its README bullet.

- [ ] **Step 2: Write the failing settings test**

`tests/test_settings.py` has `TempSettingsCase` and a test asserting every SCHEMA field carries help text. Add a `TestBeatSliceSchema` case asserting the four keys exist, carry the right default and `scope == "tool"`, that `slice_no_bpm`'s choices cover exactly `{"skip", "warn", "block"}` and `slice_boundary`'s cover `{"hard", "padded"}`, and that `_coerce` accepts each shipped default and refuses `slice_no_bpm="nope"` by name (mirror the `nr_method` coercion test).

```python
class TestBeatSliceSchema(TempSettingsCase):
    def test_slice_fields_have_defaults_choices_and_tool_scope(self):
        schema = {f["key"]: f for f in settings.SCHEMA}
        self.assertEqual(schema["slice_to_beat"]["default"], False)
        self.assertEqual(schema["slice_no_bpm"]["default"], "warn")
        self.assertEqual(schema["slice_boundary"]["default"], "hard")
        self.assertEqual(schema["slice_pad_ms"]["default"], 5)
        for key in ("slice_to_beat", "slice_no_bpm", "slice_boundary", "slice_pad_ms"):
            self.assertEqual(schema[key]["scope"], "tool")
        self.assertEqual({c[0] for c in schema["slice_no_bpm"]["choices"]}, {"skip", "warn", "block"})
        self.assertEqual({c[0] for c in schema["slice_boundary"]["choices"]}, {"hard", "padded"})
```

- [ ] **Step 3: Run the settings test to verify it fails**

Run: `python3 -m unittest tests.test_settings.TestBeatSliceSchema -v`
Expected: FAIL with `KeyError` (fields absent).

- [ ] **Step 4: Run the settings test to verify it passes**

Run: `python3 -m unittest tests.test_settings.TestBeatSliceSchema -v`
Expected: PASS (after Step 1's schema change it should already pass; run to confirm).

- [ ] **Step 5: Add the four README `### Advanced` bullets**

Column-0 bullets in the exact parsed shape `- \`key\` (\`shown\`)` — `slice_to_beat` renders `off`, `slice_no_bpm` renders `warn`, `slice_boundary` renders `hard`, `slice_pad_ms` renders `5` — each followed by prose answering default, effect, when-to-change, the field's own `help` words, and `scope: tool`. Insert after the existing `nr_model` bullet, preserving schema order within the group (run `tests/test_settings.py::TestReadmeSettingsSection`).

- [ ] **Step 6: Run the README settings test**

Run: `python3 -m unittest tests.test_settings -v`
Expected: PASS, including `TestReadmeSettingsSection` and `TestSchema.test_every_field_is_documented_for_hover` for the four new fields.

- [ ] **Step 7: Commit**

```bash
git add tools/settings.py tests/test_settings.py README.md
git commit -m "feat: add beat-slicing settings to the schema"
```

---

### Task 4: Ornamentation — sample_tool orchestration and SampleResult.warning

**Files:**
- Modify: `tools/sample_tool.py` (imports, `SampleResult`, `_apply_beat_slice`, `build_sample`)
- Modify: `tests/test_sample_tool.py`

**Interfaces:**
- Consumes: `detect_beats`, `choose_bounds`, `reslice`, `MIN_BEATS`, `NO_BPM_MESSAGE` from `tools/beat_slice.py`; `wav_seconds(path)` from `sample_tool`.
- Produces:
  - `SampleResult` gains `warning: str | None = None` and `as_dict()` emits `"warning"`.
  - `_apply_beat_slice(segment: Path, cfg: dict) -> str | None` — returns the warning string or `None`; raises `RuntimeError` for `block`.

- [ ] **Step 1: Write the failing orchestration test (fake aubio writes no beats -> warn)**

Use a fake aubio script that prints nothing (so detection returns `[]`, under the 4-beat floor) and a `CardFixture`/build_sample harness mirroring `TestFinalLimiterReal`. Assert the full `build_sample` runs, installs, and returns a result whose `as_dict()["warning"] == "No BPM found, cannot reslice"`.

```python
def test_beat_slice_warn_when_no_beats_sets_warning_and_still_installs(self):
    # cfg with slice_to_beat=True, slice_no_bpm="warn"; fake aubio prints nothing
    result = build_sample(url, start, end, ...)   # mirror TestFinalLimiterReal wiring
    self.assertEqual(result.as_dict()["warning"], "No BPM found, cannot reslice")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m unittest tests.test_sample_tool.<CaseName>.test_beat_slice_warn_when_no_beats_sets_warning_and_still_installs -v`
Expected: FAIL (`"warning" not in result` / `AttributeError`).

- [ ] **Step 3: Implement `_apply_beat_slice` and the `SampleResult.warning` field in `tools/sample_tool.py`**

Add `import beat_slice as bs` beside the existing `ac`/`sk_install`/`st_settings`/`wb` imports; add `warning: str | None = None` to the `SampleResult` dataclass and `"warning": self.warning` to `as_dict()`.

`_apply_beat_slice(segment, cfg)`:
- If `not cfg.get("slice_to_beat")`: return `None`.
- `beats = bs.detect_beats(segment)` (raises a clear RuntimeError if aubio is absent).
- `total = wav_seconds(segment)`.
- `bounds = bs.choose_bounds(beats, cfg.get("slice_boundary"), cfg.get("slice_pad_ms", 0), total)`.
- If `bounds is None`: read `slice_no_bpm` — `"skip"` -> `None`, `"block"` -> `raise RuntimeError(bs.NO_BPM_MESSAGE)`, else (`"warn"`) -> return `bs.NO_BPM_MESSAGE`.
- Else `reslice` to a temp sibling, `os.replace` onto `segment`, return `None`.

`reslice` writes `segment.with_name(segment.stem + ".beat.WAV")` then `os.replace(tmp, segment)` so downstream deliver sees the same path.

- [ ] **Step 4: Wire the call into `build_sample`**

After `apply_final_filters(...)` and before the deliver/placement block, call `warning = _apply_beat_slice(segment, cfg)` and stash it on the result so `as_dict()` carries it. When the call raises (block, or missing aubio), nothing downstream runs and the exception propagates to the CLI's existing `except (ValueError, RuntimeError)` path.

- [ ] **Step 5: Run the orchestration test to verify it passes**

Run: `python3 -m unittest tests.test_sample_tool.<CaseName> -v`
Expected: PASS.

- [ ] **Step 6: Add and run the block and skip cases**

Two more tests: `slice_no_bpm="block"` with an empty-beats fake aubio asserts `build_sample` raises `RuntimeError("No BPM found, cannot reslice")`; `slice_no_bpm="skip"` asserts a `None` warning and successful install. A final test with a real-beat fake aubio (printing 4+ beats inside the segment) asserts the delivered `segment` length equals `last_beat - first_beat` (hard cut), proving the re-slice actually happened.

- [ ] **Step 7: Run the full python suite**

Run: `/usr/bin/python3 -m unittest discover -s tests -t .`
Expected: PASS, count risen by the new tests in test_beat_slice.py, test_settings.py, test_sample_tool.py.

- [ ] **Step 8: Commit**

```bash
git add tools/sample_tool.py tests/test_sample_tool.py
git commit -m "feat: apply beat slicing to the finished sample in build_sample"
```

---

### Task 5: Surfaces the warn outcome in the widget

**Files:**
- Modify: `extension/content.js` (the `pollJob` "done" branch)
- Modify: `tests/test_widget_wiring.mjs`

**Interfaces:**
- Consumes: `job.result.warning` (string), which the bridge already stores verbatim from the tool's `--json` output.
- Produces: on "done" with a non-empty `result.warning`, the status surfaces the warning after the success line; no persisted state.

- [ ] **Step 1: Write the failing wiring test**

Follow the harness's extract-and-execute pattern (`assert.ok(src)` before slicing, execute through `new Function` with plain-object stubs, assert behaviour). Assert that a done result `{warning: "No BPM found, cannot reslice"}` calls the status writer with a string containing the warning, and that a done result with no `warning` walks the unchanged success path.

- [ ] **Step 2: Run it to verify it fails**

Run: `node --test tests/test_widget_wiring.mjs`
Expected: the new test FAILS (warning not yet rendered).

- [ ] **Step 3: Implement the warn branch in `extension/content.js`**

In `pollJob`'s done handler, after the existing success status is composed, if `job.result && job.result.warning` exists, append the warning (newline + the string) to the status text (or emit a second status call with the warning wording). Keep `block` out of this branch: block never reaches "done" because the tool exits nonzero and the existing error branch already shows the stderr tail.

- [ ] **Step 4: Run it to verify it passes, mutation-check the branch**

Run: `node --test tests/test_widget_wiring.mjs`
Expected: PASS. Confirm the assertion extracts the function's last statement (no truncated-body silent pass) and that inverting the `&&` to a truthy-only guard makes the named test fail.

- [ ] **Step 5: Commit**

```bash
git add extension/content.js tests/test_widget_wiring.mjs
git commit -m "feat: surface the beat-slice warning in the widget"
```

---

### Task 6: Suites, documentation sync, and stdlib fence

**Files:**
- Modify: `README.md` (test-count bullets)

**Interfaces:**
- Consumes: all prior tasks.
- Produces: green full suite on `/usr/bin/python3` and node, and a README whose per-file counts sum to the discovered total.

- [ ] **Step 1: Recount the headline numbers from the real runner**

Run each module dotted (`python3 -m unittest tests.test_beat_slice`, `tests.test_settings`, `tests.test_sample_tool`, ...) and `node --test tests/*.mjs`; cross-check the sum against the `Ran N tests` line from `discover -s tests -t .`. Never retype a count from memory.

- [ ] **Step 2: Update the README test-count bullets**

Write the measured per-file totals so they sum to the discovered total.

- [ ] **Step 3: Verify the stdlib fence still sees only stdlib + first-party**

`tools/beat_slice.py` imports `shutil`, `subprocess`, `math`, `pathlib.Path` and first-party `wav_builder` only. Confirm `tests/test_stdlib_only.py` has no new allowed-vs-third-party drift by running it directly.

- [ ] **Step 4: Run the full suite twice, once per interpreter**

Run: `/usr/bin/python3 -m unittest discover -s tests -t .` and `node --test tests/*.mjs`
Expected: all green; the higher of the two python interpreters matters only insofar as `/usr/bin/python3` is the bridge's interpreter, so report that one's pass explicitly.

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: sync test counts for beat slicing"
```
