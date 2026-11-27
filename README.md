# SpottyTube

> Users are responsible for their content and for complying with platform
> terms and copyright law!

![SpottyTube panel on a YouTube video](spotty_screen.png)

A browser extension (Chrome / Brave / Chromium, Manifest V3) that puts a small
panel on a video page and hands the video URL plus a start/end range to
`tools/sample_tool.py`, which cuts the clip and puts it on a Spotykach tape.

A browser cannot start a process, so this is two pieces:

    extension/   the widget on the page (no host access beyond 127.0.0.1)
    bridge/      a 127.0.0.1 HTTP server that runs tools/sample_tool.py

The pipeline is site-agnostic. A URL is a string, and `--source` is an option
(default `generic`), not a hardcoded provider: `--source youtube` only means
"parse the video id out of this URL so the cache filename is recognisable".
Nothing else about the run depends on where the media came from.

Delivery prefers USB. If no Spotykach answers over USB, a mounted card is used
instead. The widget shows a red dot when neither is possible.

## Install

Novices: read [INSTALL](INSTALL), which walks through it step by step.

    cd ~/Documents/git/spottytube-ext
    ./install.sh                     every Chrome, Brave or Chromium found
    ./install-chrome.sh              Chrome only (or --browser brave|chromium)

That links `~/.local/bin/spottytube-bridge`, writes
`~/.config/systemd/user/spottytube-bridge.service`, starts it and waits for
`/health`. Re-running is safe, and nothing is downloaded: the checkout is
complete as cloned.

`tools/wav_builder.py` is vendored. It is maintained in the Spotykach skill
repo, and the copy here is the one the tool and the bridge both import, so
there is exactly one file to keep correct and nothing to fetch at install
time. To pick up a newer upstream, copy it in and run the tests.

Browser detection lives in `installers/browsers.sh` and finds native packages,
snaps and Flatpaks by executable name; when the same name exists twice, PATH
order decides, exactly as it would if you typed the command. `--browser` takes
`chrome`, `brave`, `chromium`, `auto` (the default) or a comma separated list,
and an unknown name is an error rather than a silent no-op.

Then load the extension: `chrome://extensions` (Chrome, Chromium) or
`brave://extensions` (Brave) -> Developer mode -> Load unpacked ->
`~/Documents/git/spottytube-ext/extension`. The installer prints these lines for
the browsers it actually found.

If a browser came from Flatpak, `install.sh` also runs
`flatpak override --user --filesystem=<this repo>:ro <app-id>` for each Flatpak
browser it found (`com.google.Chrome`, `com.brave.Browser`,
`org.chromium.Chromium`). Without it the sandbox cannot see `~/Documents` and
"Load unpacked" fails with no message at all. Restart that browser after the
first install for it to take effect.

The Flatpak build shares the network namespace, so it reaches
127.0.0.1:8765 normally.

    ./install.sh --uninstall     stop and remove the service and the link
    ./install.sh --port 8766     different port (also edit manifest.json
                                 host_permissions and extension/background.js BASE)

## USB (preferred transport)

Needs firmware 1.4.0 or newer (`spotykach` skill, `references/firmware.md`) and
a udev rule so the CDC port is usable without root:

    # /etc/udev/rules.d/99-spotykach-cdc.rules
    SUBSYSTEM=="tty", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="5740", MODE="0666"

The tool nudges the unit into transfer mode with a SysEx message, opens the
`/dev/ttyACM*` node that appears, and writes the slot. Transfer mode times out
on its own within a minute; the tool re-enters it whenever a node is gone.

## Using the widget

The panel appears on `youtube.com/watch`, `/shorts/` and `/live/` pages (add
match patterns to `extension/manifest.json` for other sites; the rest of the
pipeline needs no change).

- **SET START / SET END** button, one press per index: the first click stores
  the player's current position as the in-point, the second click stores the
  out-point (and stops there), a third click starts a new pair.
- **Hold the same button** (~0.5 s) to reset both indexes.
- If you click behind the in-point, the in-point moves rather than producing an
  impossible range.
- Once the in-point is picked, an **AUTO-END IN** countdown runs 42 s of wall
  time; the last 5 s flash red every half second. If it reaches zero with no
  out-point clicked, the player's position at the elapse becomes the out-point
  automatically. Because the count is wall time while the deck cap is on
  stored seconds, a slow rate can leave the position a hair over the cap
  (32 s of source at 0.75x stores as 42.7 s); the auto out-point is then
  trimmed to the longest whole-second range that fits, and the status says
  so. A manual SET END is never trimmed: over the cap it is flagged instead.
  Picking an out-point, holding to reset, or moving to another video stops
  the count.
- **Bump / Append** picks where on the tape chain the sample lands (see Slot
  order below). The deck choice is gone: files live in the `B G P R T Y`
  directories, not on a deck. `--tape` picks the tape the chain starts at.
- **Playback speed** slider (0.25x-2x, in 0.05 steps) says how fast the range
  was heard. The slice is cut normally and then re-recorded in a second ffmpeg
  pass, so the deck still gets an ordinary normal-speed WAV — just a longer or
  shorter one: 4 s at 0.5x becomes 8 s, 6 s at 2x becomes 3 s. 1x skips the
  pass entirely.
- **SNAP TO PLAYER** keeps the slider on the rate the page's `<video>` element
  is actually running at (`playbackRate`, read every 0.5 s, no site API). Turn
  it off and the slider stays where the player left it. The button is
  `aria-pressed`; the readout shows the current rate.
- **Send to deck** posts the range; the status line reports where the sample
  landed on the unit — its tape directory and slot, like `G/2.WAV` — not the
  generated segment file on this machine.
- Green dot: the bridge is up and something can take a sample. Red dot: the
  bridge is down, or neither USB nor a card is available (the Send button is
  disabled). It re-checks every 20 s.
- **Gear button** opens the settings panel: every setting from
  `tools/settings.py` rendered from the schema the bridge serves (labels,
  controls, ranges, hover help). Save validates, stores, applies without a
  restart and closes the panel like Cancel; **Settings** below writes the same
  list out in prose, with suggested
  uses. **Restart bridge service** at the bottom restarts the systemd unit
  so the bridge reloads the code on disk — use it after editing bridge or tool
  code, since a running service keeps the build it started with. The panel
  waits for the service to come back and re-syncs, showing the new version.
- Drag the title bar to move it; position, save mode, collapsed state, speed
  and snap persist.

Times are whole seconds; `sample_tool` parses `SS`, `MM:SS` or `HH:MM:SS`.
The 42 s ceiling applies to what gets STORED, not to the range you pick:
stored seconds are the range divided by the speed you heard it at. Slowing
down does not buy a longer selection — at 0.5x only 21 s of source fits in
42 s of tape. Speeding up lets a longer source range fit the same tape — at
1.5x, 63 s of source becomes a 42 s sample: the cut is taken at normal speed
and re-timed at 1.5x when it is built. The widget, the bridge and the tool all
refuse a range whose stored length is over 42 s.

## Settings

One JSON file holds every user-adjustable value:
`~/.config/spottytube/settings.json` (`SPOTTYTUBE_SETTINGS` overrides the path).
The gear panel is a view of it. The bridge serves the schema from
`tools/settings.py` over `GET /settings` and validates whatever the panel posts
back against that same schema, so the panel, the tool and this list cannot
describe different things. Saving validates every value, stores the file
atomically and applies it with no restart.

Who reads a key, and when a change lands:

- **tool** — `sample_tool.py` reads the file on every build, so a change takes
  effect on the next Send to deck.
- **widget** — the browser widget reads it for the next save, countdown tick or
  fresh page: placement, transport, speed limits and countdown timing change
  live.
- **card** — written to `SK/config.txt` when a save changes a device value with
  the SD card mounted; the unit picks the values up at its next power-up with
  that card in.
- **bridge** — read by the running bridge process and applied to the next job.

Each key is listed with the value it ships, what it does, and when it is worth
changing; the same words are the hover help in the panel. An empty text value
means "use the built-in default".

### Audio pipeline

- `download_format` (`bestaudio`) — the yt-dlp format selector used when the
  source is downloaded. m4a/AAC is usually the highest-quality audio a video
  page offers, webm/Opus is the common alternative, and plain `bestaudio` lets
  the downloader pick. Applies to the next download. Suggested: leave it unless
  something downstream handles one of the containers badly.
- `loudnorm` (`off`) — runs ffmpeg `loudnorm` inside the cut chain, so every
  sample lands on the tape at the same perceived volume. One more stage in the
  chain, not an extra ffmpeg run, and the dynamics of quiet passages survive at
  the default target. Suggested: on when you tape sources of very different
  loudness together, off when you are sampling one record or one video.
- `loudnorm_i` (`-16.0`) — the integrated loudness target in LUFS, used when
  `loudnorm` is on. Lower is quieter, higher is louder. Suggested: -16 for
  music, -18 to -20 for speech you want to sit under other material.
- `speed_method` (`atempo`) — how a rate other than 1x is baked into the
  sample. `atempo` time-stretches and keeps the original pitch; `varispeed`
  shifts pitch with the rate, like slowing a tape. Both produce the same
  duration on the deck. Suggested: `atempo` for anything with a tune you want
  to keep, `varispeed` when the pitch shift is the effect itself.

### Save defaults

- `default_mode` (`append`) — where a new sample lands. `bump`: slot 1 of the
  tape, with every occupied slot behind it moving one place down the
  `B G P R T Y` chain and whatever falls off `Y/6` archived rather than deleted
  (see Slot order). `append`: the first free slot, nothing moves. Suggested:
  `append` while you are collecting samples, `bump` when you want the newest
  one always first.
- `default_tape` (`B`) — the tape the chain starts at. Tapes play in device
  order `B G P R T Y`, six slots each; in `append` mode the first free slot on
  any tape may take the sample, so this is a starting point rather than a hard
  target. Suggested: set the tape you are working on.
- `default_transport` (`auto`) — how samples reach the instrument. `auto` uses
  USB when the unit answers and falls back to a mounted card; `usb` and `card`
  force one path and fail instead of falling back. Suggested: leave `auto`;
  force a path when you would rather see a failure than a silent fallback.

### Widget

- `danger_seconds` (`5`) — how many seconds are left on the auto-end countdown
  when it turns red and starts blinking. The countdown itself always spans the
  deck's 42 s loop ceiling. Suggested: 5 is a fair warning; raise it if you
  usually need to hear the end twice before deciding.
- `flash_ms` (`500`) — the blink phase of the red countdown in milliseconds:
  on for this long, off for this long. Suggested: 500 is a calm pulse, lower is
  frantic.
- `speed_min` (`0.25`) — the lower bound of the speed slider. The toolchain
  accepts 0.25x-2x, so the slider can only be narrowed, never widened past that
  envelope. Suggested: raise it above 0.5 to keep half-speed saves off the
  tape.
- `speed_max` (`2.0`) — the upper bound of the speed slider (hardware ceiling
  2x). Suggested: lower it so an accidental fast save cannot land.
- `speed_step` (`0.05`) — how much one notch of the slider changes the rate.
  Suggested: 0.05 gives the whole 0.25x-2x range in the documented steps;
  coarser steps cross it faster, finer steps reach exact rates.
- `speed_default` (`1.0`) — the rate a fresh page settles on, inside the
  minimum and maximum above. Snapping to the player overrides it until snap is
  turned off. Suggested: 1.0 unless you almost always sample at one other rate.

### Device (config.txt)

- `mid_ch_a` (`1`) — the MIDI channel deck A listens on, 1-16; it must differ
  from deck B. Written to `SK/config.txt` only when a save changes it and the
  card is mounted; the unit picks it up at its next power-up. Suggested: leave
  both channels alone unless the unit shares a MIDI path with other gear.
- `mid_ch_b` (`2`) — the MIDI channel deck B listens on, 1-16; it must differ
  from deck A. Written the same way, picked up at the next power-up.
- `mid_ps_a` (`off`) — lets MIDI Start/Stop messages control deck A playback.
  Off means MIDI notes still trigger slices but transport messages are ignored.
  Suggested: on when a DAW or a sequencer is driving the set.
- `mid_ps_b` (`off`) — the same for deck B.
- `pre_load` (`on`) — pre-loads sample audio into RAM so slices trigger
  instantly. Suggested: leave it on unless a slow card makes loading click;
  after changing it, power-cycle with the card in and load each slot once.
- `slc_mn_a` (`off`) — deck A Slice mode: mono, no polyphony, so a newly
  triggered slice cuts the previous one instead of layering over it. Suggested:
  on when you want one-shots that do not smear into each other.
- `slc_mn_b` (`off`) — the same for deck B.

### Bridge and paths

- `tool_path` (`""`) — an override path to the sampling tool script (`.py`);
  empty uses the tool bundled with this install. Applied to the next job, and a
  path that does not exist is refused. Suggested: point it at a working copy
  only while iterating on the tool.
- `usb_port` (`""`) — forces the serial port used for USB transfers, for
  example `/dev/ttyACM1`; empty auto-detects the Spotykach. Applied to the next
  save without restarting the bridge. Suggested: set it when a second CDC
  device makes auto-detection pick the wrong node.
- `cache_dir` (`""`) — where downloads and cut segments are cached; empty uses
  `~/.hermes/cache/spotykach`. Suggested: point it at a bigger disk if you
  sample long sources; existing cache files are not moved.

### Advanced

The panel keeps these under a collapsed **Advanced** heading, because they
change the audio and the panel is only so tall. All of them ship neutral, with
one deliberate carve-out: `edge_fade` ships as `declick`, which by design tapers
the first and last few milliseconds of every cut. So a default build adds **no
extra ffmpeg pass** and is byte-identical to the single-cut build this tool has
always made, apart from that edge taper.

- `edge_fade` (`declick`) — shapes the cut boundary. Slicing through a loud
  waveform leaves a step at each end that clicks on every trigger: `declick`
  fades the first and last few milliseconds to kill it, `loop` fades both ends
  so they match for a click-free wrap, and `off` leaves the cut raw, which is
  the old behaviour. Suggested: leave `declick`, and reach for `loop` when the
  sample is going to be looped rather than triggered once.
- `edge_fade_ms` (`5`) — the fade length in milliseconds, symmetric at both
  ends. 5 ms is inaudible on a transient and enough to remove the click; longer
  settings soften the attack noticeably. Suggested: leave it at 5, and use
  `trim_silence` instead when the problem is a noisy start.
- `true_peak` (`off`) — a final `alimiter` pass over the re-timed sample, so
  peaks pushed past full scale by the speed change cannot clip on the deck. It
  runs as its own pass **after** the re-time rather than inside the cut, so it
  protects the stored file at every rate, including 1x where the re-time is a
  plain copy and no ffmpeg runs at all. Suggested: on when you save at rates
  away from 1x and hear the deck distort.
- `true_peak_dbtp` (`-1.0`) — the limiter ceiling in dB below full scale. The
  filter's `limit` is the linear form (`10 ** (dbtp / 20)`), and it runs with
  `level=off`, so the result is only ever attenuated to the ceiling and never
  lifted back up. Suggested: set it to `loudnorm_tp` and leave it there.
- `cut_seek` (`fast`) — how ffmpeg seeks to the cut start. `fast` jumps to the
  nearest keyframe and decodes forward, which is quick on a long source;
  `accurate` decodes from the file start and lands frame-exact, at a cost
  proportional to where the range sits. Both are safe: the de-click tapers
  follow the clock the cut actually lands on. Suggested: leave `fast`, and use
  `accurate` only when a range has to land on an exact frame.
- `loudnorm_tp` (`-1.5`) — loudnorm's true-peak ceiling in dBTP, used when
  `loudnorm` is on. -1.5 leaves headroom for lossy playback; 0 allows full
  scale.
- `loudnorm_lra` (`11`) — loudnorm's loudness-range target in LU, used when
  `loudnorm` is on. 11 suits music and keeps its dynamics; smaller numbers
  flatten quiet passages. Suggested: lower it towards 8 when you want a quiet
  intro pulled up.
- `loudnorm_mode` (`dynamic`) — `dynamic` levels while it measures, in a single
  pass; `linear` measures the cut first and then applies a fixed gain, which is
  more predictable and costs the one extra ffmpeg run in this group. Used when
  `loudnorm` is on. Suggested: `dynamic` for one-offs, `linear` for a batch you
  want to match.
- `highpass` (`off`) — a high-pass before the re-time, dropping rumble and
  handling noise below the cutoff. The deck cannot reproduce deep sub-bass, so
  this frees headroom for what it can. Suggested: on for vinyl rips, phone
  recordings and anything with traffic or air-conditioning in the room.
- `highpass_hz` (`30`) — the cutoff in Hz. 30 removes rumble without touching
  musical bass; raise it for speech or a noisy recording. Suggested: 30 for
  music, 80 to 120 for voice.
- `lowpass` (`off`) — cut everything above the low-pass cutoff. With the
  high-pass this makes a band: 200 Hz and 3 kHz leaves the range a voice lives
  in. Suggested: on alongside the high-pass for speech, phone recordings and
  anything with a hiss above the voice.
- `lowpass_hz` (`3000`) — where the low-pass starts cutting, in Hz. 3000 keeps
  speech and drops the hiss above it, and is ignored while the low-pass is off.
  Suggested: leave 3000 for voice, and raise it when speech sounds muffled.

The **Isolate audio speech** tick in the panel is a shortcut, not a setting of
its own and never saved as one: it sets the high-pass to 200 Hz and the low-pass
to 3 kHz, the band a voice lives in.

- `dc_shift` (`off`) — measures the cut's DC offset and shifts it out before
  the re-time. An offset eats headroom and shows up as a click at the
  cut edges; the measurement is one short `astats` probe. Suggested: on when a
  source clicks at both edges no matter how the fades are set.
- `resampler` (`swr`) — the resampler used when the sample rate has to change.
  `swr` is ffmpeg's own and is always there; `soxr` is higher quality when this
  ffmpeg was built with it, and is rejected loudly when it was not. The output
  is always 48 kHz stereo float32. Suggested: leave `swr`; `soxr` only matters
  when the source is not already 48 kHz.
- `dither` (`none`) — dither added while resampling. `triangular` is the usual
  choice when the bit depth drops and `triangular_hp` pushes that noise above
  the audible band, but the deck takes 32-bit float, so left at `none` the
  resample stays transparent in the ways that matter here. Suggested: leave it
  at `none`; it exists for completeness.
- `trim_silence` (`off`) — trims leading and trailing near-silence before the
  re-time, so the sample starts on its first sound. It uses a peak threshold
  rather than a gate, so quiet material can be trimmed when it sits below the
  threshold. Suggested: on when the range you picked opens with dead air.
- `trim_threshold_db` (`-50`) — what counts as silence for the edge trim, in
  dB. -50 ignores room tone; raise it towards -30 for hissier sources. Used
  when `trim_silence` is on. Suggested: leave -50 unless a hiss survives at the
  head of the trim.
- `noise_reduction` (`off`) — denoise before the re-time, for tape hiss, room
  noise or broadband hum. One more filter stage in the cut chain, and it can
  dull bright material. Suggested: on for a noisy source you cannot re-record,
  and listen to one sample before taping a batch.
- `nr_method` (`afftdn`) — which denoiser to use. `afftdn` is ffmpeg's spectral
  denoiser and needs no external files; `anlmdn` is the non-local-means
  denoiser, also self-contained; `arnndn` is rnnoise, better on speech, and
  runs one of five voices bundled with the tool. Used when `noise_reduction`
  is on. Suggested: `afftdn` for hiss and hum, `anlmdn` for a steady hiss,
  `arnndn` for voice.
- `nr_model` (``) — which rnnoise voice `arnndn` runs. The picker offers the
  five voices bundled with the tool, and each choice's label says what that
  voice was trained on. `arnndn` runs the voice you pick; `afftdn` and
  `anlmdn` ignore it. A value posted from the panel that is not a bundled
  name is refused when you save, and the field is named in the error; a value
  hand-edited into the file that is not a bundled name never reaches the
  panel, because the load falls back per key and drops it to the empty
  default.
- `slice_to_beat` (`on`) — trim the finished sample to its first and last
  detected beat so a loop lands on the grid. The tool runs aubio's beat
  tracker over the cut last, after the re-time and the final filters, so a
  speed change cannot move the beats. On by default: it costs one extra
  aubio pass per sample, and a sample with no clear pulse is sent uncut (see
  `slice_no_bpm`).
- `slice_no_bpm` (`warn`) — what happens when the beat tracker finds no
  usable BPM: fewer than four beats, or a first-to-last beat span under half
  a second. `skip` sends the sample uncut with no message; `warn` sends it
  uncut and shows "No BPM found, cannot reslice"; `block` refuses to send it.
  Used when `slice_to_beat` is on, and its row is greyed out until then.
- `slice_boundary` (`hard`) — how the beat-derived cut meets the sample's own
  edges. `hard` starts on the first beat and ends on the last; `padded` keeps
  the lead-in and tail set below so the loop breathes. Used when
  `slice_to_beat` is on, and its row is greyed out until then.
- `slice_pad_ms` (`5`) — milliseconds of lead-in and matching tail kept beyond
  the first and last beat for a `padded` cut, clamped to the file. Raise it
  for a looser loop, lower it for a tighter one. Used when `slice_to_beat` is
  on and `slice_boundary` is `padded`; the row stays greyed out until both
  hold.

**Match `loudnorm_tp` and `true_peak_dbtp`.** Loudnorm drives toward its own TP
(default `-1.5`) and the limiter caps at `true_peak_dbtp` (default `-1.0`). Two
ceilings both apply and the tighter one wins, so a limiter set *looser* than
`loudnorm_tp` cannot undo loudnorm, while one set *tighter* will re-limit
loudnorm's output. Set them to the same number and stop thinking about it;
nothing reconciles them for you.

The chain order is fixed: `highpass → lowpass → dcshift → <noise reduction> →
silenceremove → afade → loudnorm → aresample`, and the true-peak limiter runs
afterwards as its own final pass. Each setting that needs a pass costs wall time
proportional to the sample, so a default build stays the quick one.

## Slot order

Tapes play in device order B G P R T Y, six slots each, files always named
`1.WAV` .. `6.WAV` — the whole device is one chain of 36 slots.

**Bump**: the new sample takes slot 1 of the target tape and
every occupied slot behind it moves one place down the chain — `1.WAV` bumps
into `2.WAV`, `B/6.WAV` bumps into `G/1.WAV`, and so on, each file renamed to
its new slot. A file can only fall off the chain when the very last slot,
`Y/6`, is full; otherwise it always lands in the next directory in order.

**Append** (the default): the new sample takes the first free slot on the
chain and nothing moves. A completely full chain is reported as an error
instead of bumping.

Nothing is deleted. The file pushed off `Y/6` is renamed with a timestamp and
saved to the card ROOT as `<tape>_6_<YYYYmmdd-HHMMSS>.WAV`; over USB, where
there is no filesystem to write to, a mounted card is still used for the
archive, and with no card at all it goes to
`~/.hermes/cache/spotykach/archive/`.

Moves run from the far end backwards and each write is read back and compared
before the next move, so an interruption leaves a duplicate rather than
silence.

## Bridge API

    GET  /health        {"ok":true,"version":"0.2.0","tool_present":true,"card":"/media/...","pending_jobs":0}
    GET  /status        {"ok":true,"ready":true,"transport":"usb","usb":{...},"card":{...}}
    GET  /card          runs `sample_tool.py --check-card`
    GET  /settings      {"ok":true,"settings":{...},"schema":[...],"sections":[...]}
    POST /settings      partial or full values -> validated, stored, hot-applied
                        {"ok":true,"settings":{...},"card_config":...,"restart_needed":false}
    POST /restart       answers {"ok":true,"restarting":true}, then exits 70 so
                        systemd's Restart=on-failure reloads the code on disk;
                        409 when not running under systemd (manual run)
    POST /sample        {"url","start","end"} -> 202 {"job_id"}
                        optional: "mode" ("bump"|"append"), "speed",
                        "source" ("generic"|"youtube"), "transport"
                        ("auto"|"usb"|"card"), "tape" ("B".."Y"),
                        "deck" ("a"|"b", legacy pass-through)
    GET  /jobs/<id>     {"status":"queued|running|done|error","result":{...},"stderr":"..."}

Jobs run one at a time on purpose: two downloads would fight over the same
cache and the same tape. A job lives as long as the service.

The transport is opened after the slice exists, not before the download. A
Spotykach in transfer mode falls back to its MIDI-only USB identity after a few
seconds of silence, and the tty node leaves with it, so holding it open across a
download plus an ffmpeg pass ends in `Input/output error` on the next write. If
the unit drops anyway, the job is retried once as long as nothing has reached
the tape yet; a drop after a write is reported instead, because the bump down
the chain is not idempotent.

Bound to 127.0.0.1 only. Cross-origin reads are allowed only from
`chrome-extension://` origins; JSON POSTs only. Set `SPOTTYTUBE_TOKEN` (with
`--token` in the unit) to also require `X-Spottytube-Token`.

## Tests

    python3 -m unittest discover -s tests -t . -v       # 317 python tests
    node --test tests/test_selection.mjs \
                tests/test_speed.mjs \
                tests/test_widget_wiring.mjs            # 82 widget tests

`-t .` matters: without it the modules import top-level and `tests/__init__.py`
never runs, so the suite reads your real settings file instead of the pinned
temp path. Test modules import as `tests.test_x` on purpose.

- `tests/test_audio_chain.py` — 61 tests: the pure `-af` filter builder
  (`tools/audio_chain.py`) with no ffmpeg in sight, so a change to the chain
  order (highpass, lowpass, dc-shift, denoise, trim, fades, loudnorm,
  resample) or to a flag fails here instead of only showing up as a quietly
  different WAV.
  Pins the neutral build reproducing the legacy hardcode, the `anlmdn`
  method, the bundled-voice path resolution, the arnndn model refusal, the
  true-peak limiter staying out of the cut chain, and the de-click anchors
  following the filter graph's clock origin (a taper left on a 0-based clock
  under an accurate seek zeroes the whole cut). The real-ffmpeg behaviour of
  these filters lives in `tests/test_sample_tool.py`.
- `tests/test_beat_slice.py` — 13 tests: the beat-slicing seam
  (`tools/beat_slice.py`). The parser and the bounds arithmetic are pure, so
  a stdout line read whole (never whitespace-split, or the `bpm: 120` line
  becomes a phantom beat), an under-4-beat or under-0.5 s span that must be
  refused, and the hard/padded padding arithmetic all fail here. Detection
  runs the shipped `detect_beats` against a fake `aubio` scripted on `PATH`,
  and two tests run the shipped `reslice` through real ffmpeg: the span cut
  asserting the plain float32 header and frame count, and a seek bound just
  above zero (where `str()` would emit scientific notation ffmpeg rejects).
  A third pins that a failed cut raises `RuntimeError`, not
  `CalledProcessError`, so the CLI shows one clean line. `tools/` stays
  stdlib-only: aubio is shelled out, never imported.
- `tests/test_rnnoise_models.py` — 4 tests: the bundled rnnoise voices
  (`tools/rnnoise_models.py`). Each voice name resolves to a shipped `.rnnn`
  file under `tools/rnnoise_models/`, the picker offers the empty default
  first and every voice after it, a blank value stays blank, and a value that
  is not a bundled name is passed through untouched rather than rewritten.
- `tests/test_bridge.py` — 45 tests, real HTTP against `tests/stub_tool.py`, so
  nothing downloads or touches a card.
- `tests/test_settings.py` — 48 tests: every schema field carries hover help
  and its constraints, a missing/corrupt/partial file degrades to defaults per
  key, validation enforces select choices, number ranges and cross-field rules
  (speed envelope, MIDI channels differ, `tool_path` ends in `.py`), saves are
  atomic round-trips, and `SPOTTYTUBE_SETTINGS` overrides the path. Three of
  them read the README back: every schema key has exactly one bullet in
  `## Settings`, each key sits under its own schema section and default, so the
  prose and the panel cannot drift apart.
- `tests/test_stdlib_only.py` — 2 tests: an AST scan proving every `.py`
  under `tools/` and `bridge/` imports only the standard library (or a
  sibling first-party module by bare stem), with non-vacuity floors so a
  scan that walked nothing fails instead of passing forever, plus a tree
  walk that fails on `package.json`, `node_modules/` or a `jsdom` reference
  so the node harness stays plain `node --test`.
- `tests/test_sample_tool.py` — 118 tests: time parsing, generic source ids,
  transport choice, and placement on the tape chain against a real directory
  tree (bump cascades across tapes, append finds the first free slot, only a
  full Y/6 archives to the card root, read-back check, partial tapes), plus
  the speed cut, measured by decoding the WAVs ffmpeg actually produced. The
  cut pass is decoded and measured too: the de-click taper landing on the
  first and last samples, the default chain matching the legacy cut byte for
  byte, and an accurate seek over a real opus/webm source keeping its audio
  instead of producing digital silence (which the guard refuses).
- `tests/test_install_browsers.py` — 20 tests: browser detection and filtering
  run under a throwaway PATH of stub executables, so no browser on the machine
  leaks into a result and no root is needed. Covers native, snap and Flatpak
  Chrome/Brave/Chromium, the `--browser` contract, and the exit code for a name
  that does not exist.
- `tests/test_wav_builder.py` — 6 tests: the vendored module still has the API
  this project calls, its tape set and sample format agree with the rest of the
  pipeline, exactly one copy exists in the repo, and a real ffmpeg cut converts
  to a 48 kHz stereo float32 WAV.
- `tests/test_speed.mjs` — the speed model in a `vm` sandbox: clamping to
  0.25x-2x, the `1.00x` label, stored length, snap on/off, and restoring a
  saved slider. `tests/test_selection.mjs` covers the pair state machine and
  the countdown model (decrement, 5 s danger window, 500 ms flash phase,
  expiry). `tests/test_widget_wiring.mjs` guards the contract between
  `content.js` and the files it loads: script order in the manifest, the slider
  range, the snap control, `speed` in the sample payload, `playbackRate`
  polling, persistence, the countdown wiring (arm on in-point, auto
  out-point on elapse, flash classes, teardown), the settings panel (row
  labels bound to controls, a select with no stored value falling back to the
  schema's `default`, collapsible sections, an expanded group scrolling
  with the scrollbar hidden, pinned footer, viewport clamp) and the restart
  flow (worker posts `/restart`, waits for health, re-syncs).

Nothing touches the network. The ffmpeg conversion tests skip themselves only
when ffmpeg is not on the machine; the rest always runs.

## Troubleshooting

Bridge down: `systemctl --user status spottytube-bridge` and
`journalctl --user -u spottytube-bridge -n 50`.

Red dot while the bridge answers: `GET /status` names what is missing. No USB
and no card means the sample has nowhere to go; plug the unit in (the widget
picks it up within 20 s) or mount the card.

"No provided transport is usable": `sample_tool --status` prints the USB probe
result and every card candidate it looked at. `--transport card` forces the
card and fails loudly instead of falling back.

Widget missing on a video: reload the page once after loading the extension;
content scripts are injected at page load, not retroactively.

"Load unpacked" shows nothing / extension never appears (Flatpak browser): the
sandbox cannot see `~/Documents`. Run `./install.sh` again (it grants a
read-only override for this folder to every Flatpak browser it found) and
restart that browser completely.

`PermissionError` on `/recovery`: system vfat mounts that cannot be read are
skipped instead of aborting the card scan.

"is digital silence (every sample zero)": the cut came out the right length but
with no audio in it, so it is refused instead of travelling to the deck (a
silent sample looks like a build that worked). Check the source range first;
if the source is audible there, the fault is the cut filters. This is how the
`cut_seek: accurate` + edge-fade bug shows up in old logs.

## Layout

    bridge/spottytube_bridge.py    HTTP bridge, stdlib only
    install.sh                     installer: deps, link, service, browser setup
    install-chrome.sh              Chrome entry point (install.sh --browser chrome)
    installers/browsers.sh         native/snap/Flatpak detection, sourced by install.sh
    INSTALL                        step by step guide for a first install
    tools/sample_tool.py           URL + range -> a slot on the instrument
    tools/beat_slice.py            aubio beat detection + ffmpeg beat re-slice (stdlib only)
    tools/settings.py              settings schema + store, read by bridge and tool
    tools/sk_usb.py                USB transport (raw termios + amidi, stdlib)
    tools/sk_install.py            transport contract + tape-chain placement
    tools/wav_builder.py           vendored float32 writer (Spotykach skill repo)
    extension/manifest.json        MV3 content script
    extension/background.js        all fetches to the bridge happen here
    extension/content.js           the widget (shadow DOM, no page CSS leaks)
    extension/selection.js         start/end state machine + countdown (tested in node)
    extension/speed.js             playback-rate model (tested in node)
    tests/                         bridge, tool, settings and widget tests

## Licence

Public domain, Unlicense. See [LICENCE](LICENCE). This covers the code in this
repository only; media you sample stays yours to account for.
