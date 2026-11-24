# Advanced Audio Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 9 topics of ffmpeg-oriented audio control to SpottyTube, surfaced in a collapsed "Advanced" group at the end of the ffmpeg section and applied for real in the pipeline.

**Architecture:** Schema-first. The 18 new fields go in `tools/settings.py`, which already drives the panel, the bridge and the tool. The only new UI mechanism is a collapsible section, added as metadata beside `SECTIONS` so the `(key, label)` tuple shape stays intact. The ffmpeg filter expressions are built by a new pure module, `tools/audio_chain.py`, so chain order and flag syntax are unit-testable without invoking ffmpeg; `sample_tool.py` calls it and runs the passes.

**Tech Stack:** Python 3.14 (stdlib only in `tools/`), ffmpeg 9 (resolved via `wb.ffmpeg()`), pytest, the existing `node --test` widget-wiring harness, Chrome MV3 panel in `extension/content.js`.

**Spec:** `docs/superpowers/specs/2026-11-24-advanced-audio-settings-design.md`

## Global Constraints

- Python stdlib only under `tools/`. No new dependencies.
- `SECTIONS` stays a tuple of `(key, label)` 2-tuples — `tests/test_settings.py:54` unpacks it that way. Collapse lives in a separate `SECTION_META`.
- All 18 new fields carry `"section": "advanced"` and `"scope": "tool"`.
- Output is always 48 kHz float, **2 channels**. Mono output is not offered (firmware 1.4.0 refuses 1-channel WAVs, and dual mono saves no card space).
- `MAX_LOOP_SECONDS = 42.0`, `RAW_CUT_CEILING = 84.0`, `MIN_SPEED = 0.25`, `MAX_SPEED = 2.0` are unchanged.
- A build with every new setting at its default must spawn **no extra ffmpeg pass** and produce output byte-identical to today, except `edge_fade` (default `declick`) which is the one intentional default-on behavior.
- Commit per task; each task ends with its own tests green.

## Review Focus

The spec's silence on these inputs is not permission for them to break:

1. Silence trim shrinks the segment, and `extract_segment` already refuses a short segment as "source shorter than the requested range" (`sample_tool.py:639`) — trimming must not trip that guard.
2. A segment shorter than twice the fade length: the fade-out `st` must clamp at 0, never negative.
3. The default path must not accidentally grow a third ffmpeg pass or change output bytes.
4. `noise_reduction=arnndn` with an empty `nr_model` must fail with a readable message, not inside ffmpeg.
5. The panel must still render when the bridge sends no `sections` array, and when it sends the old `[key, label]` arrays — `sectionOrder` at `content.js:643` must normalize both shapes.
6. Expanding Advanced must not clip: every row reachable by scrolling inside the 78vh panel, with no visible scrollbar and no second, nested scroll area.
7. A collapsible head is a `div`, not a button, so Enter/Space must be wired by hand — otherwise the section is mouse-only.

---

### Task 1: Advanced section — schema, bridge payload, collapsed panel

**Files:**
- Modify: `tools/settings.py` (`SECTIONS` :32, new `SECTION_META`, 18 fields appended to `SCHEMA` before the closing `]` at :270, new `sections_json()`)
- Modify: `bridge/spottytube_bridge.py:448`
- Modify: `extension/content.js` (CSS block ~:138-212 including the `.sbody` scrollbar rules :176-183, `sectionOrder` :643, `renderFields` :654, `settingsState` :548)
- Test: `tests/test_settings.py`, `tests/test_bridge.py`, `tests/test_widget_wiring.mjs`

**Interfaces:**
- Produces: `settings.SECTION_META = {"advanced": {"collapsible": True, "collapsed": True}}`.
- Produces: `settings.sections_json() -> list[dict]`, one `{"key", "label", "collapsible", "collapsed"}` per `SECTIONS` entry, defaults filled from `SECTION_META`.
- Produces: 18 new schema keys, exactly the spec's table (mono output was dropped from it). `(key, control, default, range/choices)`:
  `edge_fade` (select, `declick`, off/declick/loop), `edge_fade_ms` (number, 5, 1-200),
  `true_peak` (checkbox, False), `true_peak_dbtp` (number, -1.0, -6..0),
  `cut_seek` (select, `fast`, fast/accurate),
  `loudnorm_tp` (number, -1.5, -6..0), `loudnorm_lra` (number, 11, 1-50),
  `loudnorm_mode` (select, `dynamic`, dynamic/linear),
  `highpass` (checkbox, False), `highpass_hz` (number, 30, 0-200), `dc_shift` (checkbox, False),
  `resampler` (select, `swr`, swr/soxr), `dither` (select, `none`, none/triangular/triangular_hp),
  `trim_silence` (checkbox, False), `trim_threshold_db` (number, -50, -90..-20),
  `noise_reduction` (checkbox, False), `nr_method` (select, `afftdn`, afftdn/arnndn),
  `nr_model` (text, `""`).
- Produces: `settingsState.collapsed` (Map key -> bool) in `content.js`; `.ssec.toggle` / `.sgroup`; the toggle is keyboard-operable and carries `aria-expanded`.
- Scroll contract: `.sbody` is the panel's only scroll container (`overflow-y: auto; min-height: 0; flex: 1 1 auto`, inside a `max-height: 78vh` flex panel), so an expanded Advanced group is reachable end to end. Its scrollbar stays hidden (`scrollbar-width: none`, `::-webkit-scrollbar { width: 0; height: 0; }`). No nested scroller: `.sgroup` must never get its own `max-height`/`overflow`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_settings.py
def test_advanced_sits_right_after_audio():
    names = [key for key, _ in st.SECTIONS]
    assert names[names.index("audio") + 1] == "advanced"

def test_every_new_field_defaults_match_the_spec():
    defaults = st.defaults()
    assert defaults["edge_fade"] == "declick"      # the one default-on
    assert defaults["edge_fade_ms"] == 5
    assert defaults["true_peak"] is False
    assert defaults["true_peak_dbtp"] == -1.0
    assert defaults["cut_seek"] == "fast"
    assert defaults["loudnorm_tp"] == -1.5
    assert defaults["loudnorm_lra"] == 11
    assert defaults["loudnorm_mode"] == "dynamic"
    assert defaults["resampler"] == "swr"
    assert defaults["dither"] == "none"
    assert defaults["nr_model"] == ""

def test_new_fields_coerce_and_reject(tmp_path):
    merged, errors = st.validate({"edge_fade_ms": 5.4, "true_peak_dbtp": -0.5}, base=st.defaults())
    assert errors == []
    assert merged["edge_fade_ms"] == 5 and merged["true_peak_dbtp"] == -0.5
    _, errors = st.validate({"edge_fade": "declik"}, base=st.defaults())
    assert any("edge_fade must be one of" in e for e in errors)
    _, errors = st.validate({"loudnorm_lra": 99}, base=st.defaults())
    assert any("loudnorm_lra must be between" in e for e in errors)

def test_sections_json_marks_advanced_collapsed():
    sections = {entry["key"]: entry for entry in st.sections_json()}
    assert sections["advanced"]["collapsed"] is True
    assert sections["advanced"]["collapsible"] is True
    assert sections["audio"]["collapsible"] is False
    assert [e["key"] for e in st.sections_json()] == [k for k, _ in st.SECTIONS]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_settings.py -k "advanced or new_field or sections_json" -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'sections_json'` / `KeyError: 'edge_fade'`.

- [ ] **Step 3: Implement the schema and section metadata in `tools/settings.py`**

Add `("advanced", "Advanced")` to `SECTIONS` immediately after the `audio` entry. Add below it:

```python
# Rendering hints for the panel, keyed by section. Kept out of SECTIONS so the
# (key, label) tuple shape the tool and the bridge rely on is unchanged.
SECTION_META = {"advanced": {"collapsible": True, "collapsed": True}}
```

Append the 18 field dicts to `SCHEMA` (values verbatim from the spec's table; each is the existing field shape: `key`, `section`, `scope`, `label`, `control`, `default`, `help`, plus `choices` for selects and `min`/`max`/`step` for numbers). Add after `defaults()`:

```python
def sections_json():
    """SECTIONS plus its rendering hints, for GET /settings."""
    out = []
    for key, label in SECTIONS:
        entry = {"key": key, "label": label, "collapsible": False, "collapsed": False}
        entry.update(SECTION_META.get(key, {}))
        out.append(entry)
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_settings.py -v`
Expected: PASS, including the existing section assertion at :54.

- [ ] **Step 5: Serve the object payload from the bridge**

In `bridge/spottytube_bridge.py`, change the `/settings` body line
`"sections": [list(s) for s in app_settings.SECTIONS],` (:448) to
`"sections": app_settings.sections_json(),`.

Add to `tests/test_bridge.py`:

```python
def test_settings_sections_are_objects_with_collapse_flags(client):
    payload = client.get("/settings").json()
    advanced = next(s for s in payload["sections"] if s["key"] == "advanced")
    assert advanced["collapsed"] is True and advanced["label"] == "Advanced"
    assert advanced["key"] in {f["section"] for f in payload["schema"]}
```

Run: `pytest tests/test_bridge.py -v` — Expected: PASS.

- [ ] **Step 6: Render the collapsed group in `extension/content.js`**

`sectionOrder` (:643) must return objects: when `settingsState.sections` is non-empty it is already a list of `{key,label,collapsible,collapsed}` (guard with `entry.key ?? entry[0]` so an old-style `[key,label]` array still works); the schema-derived fallback pushes `{key: field.section, label: field.section, collapsible: false, collapsed: false}`.

`renderFields` (:654): for each section, create the head as today; when `section.collapsible`, add class `toggle` to the head plus a `<span class="caret">`, create a `<div class="sgroup">`, append the rows into the group instead of `el.sfields`, and set `group.hidden = settingsState.collapsed.get(section.key) ?? section.collapsed`. The head's click handler toggles `group.hidden` and stores the new value in `settingsState.collapsed` (a Map added to the settings state object alongside `inputs`). CSS: `.ssec.toggle { cursor: pointer; }`, `.ssec .caret { float: right; }`, `.sgroup[hidden] { display: none; }`.

The widget harness does **not** simulate the DOM — it reads `content.js` as text and asserts the wiring contract (see its header comment). Assert on the source, as the rest of that file does:

```js
test("the advanced group is a collapsible section driven by the bridge", () => {
  assert.match(content, /section\.collapsible/);
  assert.match(content, /classList\.add|className = section\.collapsible \? "ssec toggle"/);
  assert.match(content, /aria-expanded/);
  assert.match(content, /settingsState\.collapsed/);
  assert.match(content, /group\.hidden/);
});

test("an expanded group is reachable without a visible scrollbar", () => {
  assert.match(content, /\.sbody \{[^}]*overflow-y: auto/);
  assert.match(content, /scrollbar-width: none/);
  assert.doesNotMatch(content, /scrollbar-width: thin/);
  assert.match(content, /\.sbody::-webkit-scrollbar \{ width: 0; height: 0; \}/);
  assert.doesNotMatch(content, /::-webkit-scrollbar-thumb/);
  assert.doesNotMatch(content, /\.sgroup[^}]*max-height/);   // no nested scroller
});

test("a collapsed group hides its rows through the author rule", () => {
  assert.match(content, /\.sgroup\[hidden\] \{ display: none; \}/);
  assert.match(content, /new Map\(\)/);                     // collapse memory
});

test("both section payload shapes are accepted", () => {
  assert.match(content, /Array\.isArray\(entry\)/);
  assert.match(content, /entry\.key/);
});
```

The collapse behaviour itself has no live-DOM test in this harness; keep the code small enough to read at a glance, and verify it by hand in the browser once at the end of the task.

Run: `node --test tests/test_widget_wiring.mjs`
Expected: PASS (all new tests and the existing ones).

- [ ] **Step 7: Commit**

```bash
git add tools/settings.py bridge/spottytube_bridge.py extension/content.js tests/test_settings.py tests/test_bridge.py tests/test_widget_wiring.mjs
git commit -m "feat(settings): advanced section with 18 fields, collapsed by default"
```

---

### Task 2: Cut-pass filters — highpass, DC, silence trim, edge fades

**Files:**
- Create: `tools/audio_chain.py`
- Modify: `tools/sample_tool.py` (`extract_segment` :590, `wav_seconds` unchanged)
- Test: `tests/test_audio_chain.py` (new), `tests/test_sample_tool.py`

**Interfaces:**
- Produces: `audio_chain.cut_filters(cfg, seconds, dc_offset=0.0) -> str` — comma-joined ffmpeg filters for the cut pass, `""` when nothing applies.
- Produces: `audio_chain.needs_dc_probe(cfg) -> bool`.
- Consumes: `cfg` is a full settings dict (`st_settings.load()` result).

- [ ] **Step 1: Write the failing unit tests**

```python
# tests/test_audio_chain.py
import settings as st, audio_chain as ac

def cfg(**over):
    base = st.defaults(); base.update(over); return base

def test_default_edge_fade_is_a_five_ms_declick():
    assert ac.cut_filters(cfg(), 10.0) == "afade=t=in:st=0:d=0.005,afade=t=out:st=9.995:d=0.005"

def test_edge_fade_off_and_loop():
    assert ac.cut_filters(cfg(edge_fade="off"), 10.0) == ""
    assert ac.cut_filters(cfg(edge_fade="loop", edge_fade_ms=20), 10.0) == \
        "afade=t=in:st=0:d=0.02,afade=t=out:st=9.980:d=0.02"

def test_fade_never_starts_before_zero():
    out = ac.cut_filters(cfg(edge_fade_ms=200), 0.1)
    assert "afade=t=out:st=0.000" in out and "-" not in out

def test_highpass_and_dc():
    assert ac.cut_filters(cfg(highpass=True), 5.0).startswith("highpass=f=30")
    assert ac.cut_filters(cfg(highpass=True, highpass_hz=45), 5.0).startswith("highpass=f=45")
    assert "dcshift=shift=-0.01" in ac.cut_filters(cfg(dc_shift=True), 5.0, dc_offset=0.01)
    assert ac.needs_dc_probe(cfg(dc_shift=True)) is True

def test_silence_trim_flags():
    out = ac.cut_filters(cfg(trim_silence=True, trim_threshold_db=-45), 5.0)
    assert "silenceremove" in out and "-45" in out

def test_order_highpass_then_dc_then_trim_then_fade():
    out = ac.cut_filters(cfg(highpass=True, dc_shift=True, trim_silence=True), 5.0, dc_offset=0.0)
    assert out.index("highpass") < out.index("dcshift") < out.index("silenceremove") < out.index("afade")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_audio_chain.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'audio_chain'`.

- [ ] **Step 3: Implement `tools/audio_chain.py`**

```python
def cut_filters(cfg, seconds, dc_offset=0.0):
    """ffmpeg -af expression for the cut pass, in fixed order."""
    parts = []
    if cfg["highpass"] and cfg["highpass_hz"] > 0:
        parts.append(f"highpass=f={float(cfg['highpass_hz']):g}")
    if cfg["dc_shift"] and dc_offset:
        parts.append(f"dcshift=shift={-float(dc_offset):g}")
    if cfg["trim_silence"]:
        thr = float(cfg["trim_threshold_db"])
        parts.append(f"silenceremove=start_periods=1:detection=peak:start_threshold={thr:g}dB")
        parts.append(f"areverse,silenceremove=start_periods=1:detection=peak:start_threshold={thr:g}dB,areverse")
    parts.extend(_edge_fades(cfg, seconds))
    return ",".join(parts)

def _edge_fades(cfg, seconds):
    mode = cfg["edge_fade"]
    if mode == "off":
        return []
    d = max(0.0, float(cfg["edge_fade_ms"]) / 1000.0)
    out_start = max(0.0, seconds - d)          # never negative (Review Focus 2)
    return [f"afade=t=in:st=0:d={d:g}", f"afade=t=out:st={out_start:.3f}:d={d:g}"]

def needs_dc_probe(cfg):
    return bool(cfg["dc_shift"])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_audio_chain.py -v` — Expected: PASS.

- [ ] **Step 5: Wire it into `extract_segment`**

`extract_segment` gains `cfg=None`. When `cfg` is not None: if `audio_chain.needs_dc_probe(cfg)`, run one `ffmpeg -i src -af astats -f null -` pass over the cut range and parse `Overall` `DC offset` from stderr into `dc_offset`; build `filters = audio_chain.cut_filters(cfg, seconds, dc_offset)`; when `filters`, pass `-af filters`. `build_sample` passes `cfg=cfg` (:929). Keep the loudnorm handling from today until Task 3.

- [ ] **Step 6: Integration tests with real ffmpeg**

Add to `tests/test_sample_tool.py`, using the existing `wav()` fixture and `_load`:

```python
def test_declick_taper_is_audible_at_the_edges(self):
    # a 0.2 s burst inside a 1 s file, cut 0.3-0.7 with a 50 ms declick
    ...  # assert first frames ramp up and last frames ramp down

def test_no_fade_the_first_frame_matches_the_source(self):
    ...  # edge_fade=off: first frame equals the source's frame at start

def test_trim_does_not_trip_the_short_segment_guard(self):
    # leading 0.4 s of silence then tone; trim_silence=True must NOT raise
    sample_tool.extract_segment(src, dst, 0.0, 1.0, cfg=cfg_with_trim)
```

Run: `pytest tests/test_sample_tool.py -v` — Expected: PASS (Review Focus 1 pinned).

- [ ] **Step 7: Commit**

```bash
git add tools/audio_chain.py tools/sample_tool.py tests/test_audio_chain.py tests/test_sample_tool.py
git commit -m "feat(audio): cut-pass highpass, DC shift, silence trim, edge declick"
```

---

### Task 3: Loudnorm completion — true peak, LRA, single vs two-pass

**Files:**
- Modify: `tools/audio_chain.py` (`cut_filters` gains a `loudnorm` step)
- Modify: `tools/sample_tool.py` (`extract_segment` :619-620, `build_sample` :891-894, `main` flags :1051-1058)
- Test: `tests/test_audio_chain.py`, `tests/test_sample_tool.py`

**Interfaces:**
- Produces: `audio_chain.loudnorm_filter(cfg) -> str` — `loudnorm=I=<i>:TP=<tp>:LRA=<lra>`, plus `linear=true` when `loudnorm_mode == "linear"`, `""` when `loudnorm` is False.
- Consumes: `cfg["loudnorm"]`, `cfg["loudnorm_i"]`, `cfg["loudnorm_tp"]`, `cfg["loudnorm_lra"]`, `cfg["loudnorm_mode"]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_loudnorm_companions_are_exposed():
    assert ac.loudnorm_filter(cfg(loudnorm=True)) == "loudnorm=I=-16:TP=-1.5:LRA=11"
    assert ac.loudnorm_filter(cfg(loudnorm=True, loudnorm_tp=-2, loudnorm_lra=7)) \
        == "loudnorm=I=-16:TP=-2:LRA=7"

def test_two_pass_mode_asks_ffmpeg_for_linear():
    assert "linear=true" in ac.loudnorm_filter(cfg(loudnorm=True, loudnorm_mode="linear"))
    assert "linear=true" not in ac.loudnorm_filter(cfg(loudnorm=True))

def test_loudnorm_off_yields_nothing():
    assert ac.loudnorm_filter(cfg(loudnorm=False)) == ""
```

- [ ] **Step 2: Run to verify they fail** — `pytest tests/test_audio_chain.py -k loudnorm -v` → FAIL.

- [ ] **Step 3: Implement `loudnorm_filter` and call it from `cut_filters`**

Build the string with `%g` formatting on the four values; append `:linear=true` for two-pass. In `cut_filters`, append `loudnorm_filter(cfg)` after the fades and before the resample step (Task 4). Delete the hardcoded `loudnorm=I=..:TP=-1.5:LRA=11` in `extract_segment` (:620) and route through `cut_filters`.

- [ ] **Step 4: Run to verify they pass** — `pytest tests/test_audio_chain.py tests/test_sample_tool.py -v` → PASS.

- [ ] **Step 5: Integration test for two-pass**

```python
def test_linear_mode_still_produces_a_valid_spotykach_wav(self):
    sample_tool.extract_segment(src, dst, 0.0, 1.0, loudnorm=True, cfg=cfg_mode_linear)
    # assert RIFF/float header and 48 kHz stereo, and duration within tolerance
```

- [ ] **Step 6: Commit**

```bash
git add tools/audio_chain.py tools/sample_tool.py tests/test_audio_chain.py tests/test_sample_tool.py
git commit -m "feat(audio): expose loudnorm true peak, LRA and single/two-pass mode"
```

---

### Task 4: Resample quality and dither

**Files:**
- Modify: `tools/audio_chain.py` (`cut_filters` resample step, new `final_filters`)
- Modify: `tools/sample_tool.py` (`build_sample` :929-933)
- Test: `tests/test_audio_chain.py`, `tests/test_sample_tool.py`

**Interfaces:**
- Produces: `aresample` options inside `cut_filters` — `resampler=soxr` when selected, `dither_method` when a dither other than `none` is selected, merged into one `aresample=` when both apply.

- [ ] **Step 1: Write the failing tests**

```python
def test_resampler_and_dither_flags():
    assert "aresample=resampler=soxr" in ac.cut_filters(cfg(resampler="soxr"), 5.0)
    assert "dither_method=triangular" in ac.cut_filters(cfg(dither="triangular"), 5.0)
    assert "aresample" not in ac.cut_filters(cfg(resampler="swr", dither="none"), 5.0)

def test_resampler_and_dither_merge_into_one_filter():
    out = ac.cut_filters(cfg(resampler="soxr", dither="triangular_hp"), 5.0)
    assert out.count("aresample=") == 1
    assert "resampler=soxr" in out and "dither_method=triangular_hp" in out
```

- [ ] **Step 2: Run to verify they fail** — FAIL (`aresample` absent from the chain).

- [ ] **Step 3: Implement**

In `cut_filters`, after loudnorm, build one `aresample=` filter: `resampler=soxr` when `resampler == "soxr"` and `dither_method=<dither>` when `dither != "none"`; append it only if at least one option applies (so the default chain still contains no `aresample`).

The post-retime final pass is introduced in Task 5, where the limiter gives it something to do.

- [ ] **Step 4: Run to verify they pass** — PASS.

- [ ] **Step 5: Integration test — the resampler flag reaches ffmpeg**

```python
def test_soxr_chain_still_produces_a_valid_spotykach_wav(self):
    sample_tool.extract_segment(src, dst, 0.0, 1.0,
                                cfg=cfg_with(resampler="soxr", dither="triangular"))
    assert wav_builder.probe(dst) is None          # existing "would the deck take it" helper
    assert sample_tool.wav_seconds(dst) == pytest.approx(1.0, abs=0.05)
```

- [ ] **Step 6: Commit**

```bash
git add tools/audio_chain.py tools/sample_tool.py tests/test_audio_chain.py tests/test_sample_tool.py
git commit -m "feat(audio): resampler and dither selection"
```

---

### Task 5: Final true-peak limiter and noise reduction

**Files:**
- Modify: `tools/audio_chain.py` (`final_filters` limiter, `cut_filters` NR step)
- Modify: `tools/sample_tool.py` (`build_sample` final pass)
- Test: `tests/test_audio_chain.py`, `tests/test_sample_tool.py`

**Interfaces:**
- Produces: `audio_chain.final_filters(cfg) -> str | None` — `None` when no post-retime pass is needed, else a comma-joined `-af` expression containing `alimiter=limit=<linear>:level=off` when `true_peak` is on, `linear = round(10 ** (true_peak_dbtp / 20), 6)`.
- Produces: `cut_filters` appends `afftdn` or `arnndn=model=<nr_model>` when `noise_reduction` is on.
- Produces: `audio_chain.NoiseModelMissing(ValueError)` raised when `nr_method == "arnndn"` and `nr_model` is empty.

- [ ] **Step 1: Write the failing tests**

```python
def test_limiter_is_a_linear_ceiling():
    assert ac.final_filters(cfg(true_peak=True, true_peak_dbtp=-1.0)) == "alimiter=limit=0.891251:level=off"

def test_default_settings_add_no_extra_pass():
    assert ac.final_filters(cfg()) is None                # Review Focus 3

def test_noise_reduction_methods():
    assert ac.cut_filters(cfg(noise_reduction=True), 5.0).count("afftdn") == 1
    assert "arnndn=model=/x/y.rnnn" in ac.cut_filters(
        cfg(noise_reduction=True, nr_method="arnndn", nr_model="/x/y.rnnn"), 5.0)

def test_arnndn_without_a_model_refuses_clearly():
    with pytest.raises(ac.NoiseModelMissing) as caught:
        ac.cut_filters(cfg(noise_reduction=True, nr_method="arnndn"), 5.0)
    assert "rnnoise model" in str(caught.value)      # Review Focus 5
```

- [ ] **Step 2: Run to verify they fail** — FAIL.

- [ ] **Step 3: Implement**

NR in `cut_filters` sits right after the DC step (before trim/fades). `final_filters(cfg)` returns `None` unless `true_peak` is on, in which case it returns the single `alimiter` term.

- [ ] **Step 4: Run to verify they pass** — PASS.

- [ ] **Step 5: Integration test — limiter actually protects the file**

```python
def test_limiter_caps_peaks_after_a_hot_retime(self):
    # varispeed up on a near-full-scale tone, then true_peak on
    peak = max(abs(struct.unpack_from("<f", data, o)[0])
               for o in range(0, len(data) - 44, 4))
    assert peak <= 10 ** (-1.0 / 20) + 1e-3
```

Run: `pytest tests/test_sample_tool.py -v` — PASS.

- [ ] **Step 6: Update `README.md`**

Document the Advanced group in the settings section: one line per setting, the default, and what it costs (each new pass adds wall time). Note that the tool always writes stereo, because the firmware refuses 1-channel files.

- [ ] **Step 7: Commit**

```bash
git add tools/audio_chain.py tools/sample_tool.py tests/test_audio_chain.py tests/test_sample_tool.py README.md
git commit -m "feat(audio): final true-peak limiter and optional noise reduction"
```

---

## Self-Review

- **Spec coverage:** the 8 delivered topics map to Tasks 2-5 (fades/trim/highpass/DC in 2, loudnorm in 3, resample/dither in 4, limiter/NR in 5); mono is dropped (spec D6). The collapsed section and schema are Task 1; the Hardware results and D1 default-on edge fade are Task 1 (default) + Task 2 (behavior).
- **Review Focus:** 1 → Task 2 step 6; 2 → Task 2 step 1 (fade clamp); 3 → Task 5 step 1; 4 → Task 5 step 1; 5/6/7 → Task 1 step 6.
- **Type consistency:** `cfg` is the full settings dict everywhere; `cut_filters(cfg, seconds, dc_offset=0.0)` keeps one signature through Tasks 2-5; `final_filters(cfg)` returns `None` (not `""`) so callers can test truthiness.
