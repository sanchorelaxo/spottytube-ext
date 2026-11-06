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

That links `~/.local/bin/spottytube-bridge`, fetches `tools/wav_builder.py`,
writes `~/.config/systemd/user/spottytube-bridge.service`, starts it and waits
for `/health`. Re-running is safe.

`wav_builder.py` is not vendored in this repository: it is maintained in the
Spotykach skill repo and `install.sh` (or `python3 tools/fetch_wav_builder.py`)
downloads it. The fetcher checks the API this project uses before replacing a
working copy, so a bad download cannot break an install. `WAV_BUILDER_URL`
overrides the source for an offline clone.

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
- **Deck A / Deck B** picks which slot on tape B that deck loads: slot 1 for
  deck A, slot 2 for deck B. `--tape` sends to another tape.
- **Send to deck** posts the range; the status line reports the transport and
  then the finished slot.
- Green dot: the bridge is up and something can take a sample. Red dot: the
  bridge is down, or neither USB nor a card is available (the Send button is
  disabled). It re-checks every 20 s.
- Drag the title bar to move it; position, deck and collapsed state persist.

Times are whole seconds; `sample_tool` parses `SS`, `MM:SS` or `HH:MM:SS`.
Segments over the 42 s deck limit are refused by the widget, the bridge and the
tool.

## Slot order

A new sample always lands in slot 1 of its tape. Everything already there
moves down one slot (1->2, 2->3, ...), so deck A's slot is the newest sample
and deck B's is the one before it.

Nothing is deleted. The sample that falls off slot 6 is renamed with a
timestamp and saved to `SK/<tape>_6_<YYYYmmdd-HHMMSS>.WAV` on the card; over
USB, where there is no filesystem to write to, a mounted card is still used for
the archive, and with no card at all it goes to
`~/.hermes/cache/spotykach/archive/`.

The shift runs high to low and each write is read back and compared before the
next move, so an interruption leaves a duplicate rather than silence.

## Bridge API

    GET  /health        {"ok":true,"tool_present":true,"card":"/media/...","pending_jobs":0}
    GET  /status        {"ok":true,"ready":true,"transport":"usb","usb":{...},"card":{...}}
    GET  /card          runs `sample_tool.py --check-card`
    POST /sample        {"url","start","end","deck"} -> 202 {"job_id"}
                        optional: "source" ("generic"|"youtube"), "transport"
                        ("auto"|"usb"|"card"), "tape" ("B".."Y")
    GET  /jobs/<id>     {"status":"queued|running|done|error","result":{...},"stderr":"..."}

Jobs run one at a time on purpose: two downloads would fight over the same
cache and the same tape. A job lives as long as the service.

Bound to 127.0.0.1 only. Cross-origin reads are allowed only from
`chrome-extension://` origins; JSON POSTs only. Set `SPOTTYTUBE_TOKEN` (with
`--token` in the unit) to also require `X-Spottytube-Token`.

## Tests

    python3 -m unittest discover -s tests -v            # 87 python tests
    node --test tests/test_selection.mjs                # 9 state-machine tests

- `tests/test_bridge.py` — 24 tests, real HTTP against `tests/stub_tool.py`, so
  nothing downloads or touches a card.
- `tests/test_sample_tool.py` — 31 tests: time parsing, generic source ids,
  transport choice, and the slot shift against a real directory tree (shift,
  archive, read-back check, partial tapes, untouched neighbours).
- `tests/test_install_browsers.py` — 20 tests: browser detection and filtering
  run under a throwaway PATH of stub executables, so no browser on the machine
  leaks into a result and no root is needed. Covers native, snap and Flatpak
  Chrome/Brave/Chromium, the `--browser` contract, and the exit code for a name
  that does not exist.
- `tests/test_wav_builder_dep.py` — 12 tests: the fetcher rejects a bad
  download without clobbering a good copy, the upstream file still has the API
  this project calls, the installed copy matches upstream, and a real ffmpeg
  cut converts to a 48 kHz stereo float32 WAV.

The two upstream tests skip themselves when GitHub is unreachable; everything
local, including the ffmpeg conversion, always runs.

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
    tools/sk_usb.py                USB transport (raw termios + amidi, stdlib)
    tools/sk_install.py            transport contract + slot shift + archive
    tools/fetch_wav_builder.py     fetches the upstream wav_builder dependency
    tools/wav_builder.py           fetched, gitignored, not vendored
    extension/manifest.json        MV3 content script
    extension/background.js        all fetches to the bridge happen here
    extension/content.js           the widget (shadow DOM, no page CSS leaks)
    extension/selection.js         start/end state machine (tested in node)
    tests/                         bridge, tool, dependency and state-machine tests

## Licence

Public domain, Unlicense. See [LICENCE](LICENCE). This covers the code in this
repository only; media you sample stays yours to account for.
