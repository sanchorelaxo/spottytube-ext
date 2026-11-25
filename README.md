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
  controls, ranges, hover help). Save validates, stores and applies without a
  restart. **Restart bridge service** at the bottom restarts the systemd unit
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

## Advanced audio settings

The settings panel groups the pipeline controls under a collapsed
**Advanced** heading (the gear panel, `tools/settings.py`). Every setting here
defaults to a neutral value, with one intentional carve-out: `edge_fade`
defaults to `declick`, which by design tapers the first and last few
milliseconds of every cut. So the promise for a build with the defaults is
that it adds **no extra ffmpeg pass** and is byte-identical to the single-cut
build this tool has always made *apart from that edge taper*. One line per
setting, its default, and what it costs:

**Edge fades** — on by default:

- `edge_fade` (`declick`) — shapes the cut boundary so the loop does not click
  where it joins. `declick` fades the first and last few milliseconds to kill
  the click; `loop` fades both ends to match for a click-free wrap; `off`
  leaves the cut raw and is the old behaviour.
- `edge_fade_ms` (`5`) — fade length in milliseconds. The fade pair is
  symmetric and equal length.

**True-peak limiter** — off by default, costs **one extra ffmpeg pass**:

- `true_peak` (`off`) — when on, a final `alimiter` pass caps the sample's
  peak. It runs as its **own pass after the cut is re-timed**, not inside the
  cut, so it protects the stored file at every speed — including 1x, where the
  re-time step is a plain copy and no ffmpeg runs at all. With it off the
  default build spawns nothing extra.
- `true_peak_dbtp` (`-1.0`) — the ceiling in dBTP; the limiter `limit` is its
  linear form (`10 ** (dbtp / 20)`). The filter uses `level=off` so the result
  is **only ever attenuated** to the ceiling — without `level=off` `alimiter`
  auto-levels back up to full scale and the ceiling is lost.
- **Match `loudnorm_tp` and `true_peak_dbtp`.** Loudnorm drives toward its own
  `TP` (default `-1.5`); the limiter caps at `true_peak_dbtp` (default `-1.0`).
  Two ceilings both apply and the tighter one wins, so a limiter set *looser*
  than `loudnorm_tp` cannot undo loudnorm — but a limiter set *tighter* will
  re-limit loudnorm's output. Set them to the same number and stop thinking
  about it; nothing reconciles them for you.

**Seek** — no extra pass either way:

- `cut_seek` (`fast`) — `fast` seeks by keyframe index and `accurate` decodes
  to the exact frame. Accurate is slower but lands on the precise sample.

**Loudness normalisation** — off by default; `linear` costs **one extra probe pass**:

- `loudnorm_mode` (`dynamic`) — `dynamic` applies `loudnorm` in one pass while
  it measures and adjusts as it goes. `linear` measures the source first (an
  extra ffmpeg pass reading the pass-1 JSON), then applies a fixed gain — no
  pumping, but a second pass.
- `loudnorm_tp` (`-1.5`) — the true-peak ceiling loudnorm targets.
- `loudnorm_lra` (`11`) — the loudness-range target, in LU.

**High-pass** — off by default, no extra pass:

- `highpass` (`off`) — a leading high-pass that removes sub-audible rumble
  before anything else in the chain.
- `highpass_hz` (`30`) — the corner frequency.

**DC offset** — off by default; costs **one probe pass** when on:

- `dc_shift` (`off`) — measures the source's DC offset (an extra ffmpeg
  `astats` pass) and shifts it out. Off leaves the cut single-pass.

**Resample / dither** — no extra pass, ever:

- `resampler` (`swr`) — `swr` (ffmpeg's built-in) or `soxr`. Only matters when
  the source is not already 48 kHz; the output is **always 48 kHz stereo**.
- `dither` (`none`) — dither added at the resample. The tool writes **float32**
  WAVs, and dither is inert on float output: it is a no-op you will not hear on
  the shipped files. Leave it at `none`; it exists for completeness.

**Silence trim** — off by default, no extra pass:

- `trim_silence` (`off`) — trims leading and trailing near-silence.
- `trim_threshold_db` (`-50`) — what counts as silence for the trim.

**Noise reduction** — off by default, no extra pass:

- `noise_reduction` (`off`) — a denoiser that runs after the DC shift and
  before the silence trim.
- `nr_method` (`afftdn`) — `afftdn` is ffmpeg's own FFT denoiser and is used
  with no parameters. `arnndn` is the rnnoise method and **needs a model
  file**.
- `nr_model` (`""`) — the `.rnnn` rnnoise model for `arnndn`. It is required
  whenever `nr_method` is `arnndn`: with it blank the build refuses up front
  with a clear error, and a path that does not load fails loudly inside ffmpeg
  (a non-zero exit surfaced as an error) rather than passing the audio through
  untouched. The tool never downloads a model for you.

The chain order is fixed: `highpass → dcshift → <noise reduction> →
silenceremove → afade → loudnorm → aresample`, and the true-peak limiter runs
afterwards as its own final pass. Each setting that needs a pass costs wall
time proportional to the sample, so a default build stays the quick one.

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

    python3 -m unittest discover -s tests -t . -v       # 173 python tests
    node --test tests/test_selection.mjs \
                tests/test_speed.mjs \
                tests/test_widget_wiring.mjs            # 72 widget tests

`-t .` matters: without it the modules import top-level and `tests/__init__.py`
never runs, so the suite reads your real settings file instead of the pinned
temp path. Test modules import as `tests.test_x` on purpose.

- `tests/test_bridge.py` — 40 tests, real HTTP against `tests/stub_tool.py`, so
  nothing downloads or touches a card.
- `tests/test_settings.py` — 39 tests: every schema field carries hover help
  and its constraints, a missing/corrupt/partial file degrades to defaults per
  key, validation enforces select choices, number ranges and cross-field rules
  (speed envelope, MIDI channels differ, `tool_path` ends in `.py`), saves are
  atomic round-trips, and `SPOTTYTUBE_SETTINGS` overrides the path.
- `tests/test_sample_tool.py` — 68 tests: time parsing, generic source ids,
  transport choice, and placement on the tape chain against a real directory
  tree (bump cascades across tapes, append finds the first free slot, only a
  full Y/6 archives to the card root, read-back check, partial tapes), plus
  the speed cut, measured by decoding the WAVs ffmpeg actually produced.
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
  labels bound to controls, collapsible sections, an expanded group scrolling
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

## Layout

    bridge/spottytube_bridge.py    HTTP bridge, stdlib only
    install.sh                     installer: deps, link, service, browser setup
    install-chrome.sh              Chrome entry point (install.sh --browser chrome)
    installers/browsers.sh         native/snap/Flatpak detection, sourced by install.sh
    INSTALL                        step by step guide for a first install
    tools/sample_tool.py           URL + range -> a slot on the instrument
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
