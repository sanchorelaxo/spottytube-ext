# SpottyTube

![SpottyTube panel on a YouTube video](spotty_screen.png)

A browser extension (Chrome / Brave, Manifest V3) that puts a small panel on a
YouTube video page and hands the current video plus a start/end range to
`tools/yt_sample.py`, which writes the clip into the Spotykach deck slot.

A browser cannot start a process, so this is two pieces:

    extension/   the widget on youtube.com (no host access beyond 127.0.0.1)
    bridge/      a 127.0.0.1 HTTP server that runs tools/yt_sample.py

`yt_sample.py` and its `wav_builder.py` dependency are vendored in `tools/`, so
this checkout runs on its own; nothing outside the project is needed (except
`yt-dlp` and `ffmpeg` on PATH).

## Install

    cd ~/Documents/git/spottytube-ext
    ./install.sh

That links `~/.local/bin/spottytube-bridge`, writes
`~/.config/systemd/user/spottytube-bridge.service`, starts it and waits for
`/health`. Re-running is safe.

Then in Brave: `brave://extensions` -> Developer mode -> Load unpacked ->
`~/Documents/git/spottytube-ext/extension`.

If Brave came from Flatpak, `install.sh` also runs
`flatpak override --user --filesystem=<this repo>:ro com.brave.Browser`.
Without it the sandbox cannot see `~/Documents` and "Load unpacked" fails with
no message at all. Restart Brave after the first install for it to take effect.

The Flatpak build shares the network namespace, so it reaches
127.0.0.1:8765 normally.

    ./install.sh --uninstall     stop and remove the service and the link
    ./install.sh --port 8766     different port (also edit manifest.json
                                 host_permissions and extension/background.js BASE)

## Using the widget

The panel appears only on `youtube.com/watch`, `/shorts/` and `/live/` pages.

- **SET START / SET END** button, one press per index: the first click stores
  the player's current position as the in-point, the second click stores the
  out-point (and stops there), a third click starts a new pair.
- **Hold the same button** (~0.5 s) to reset both indexes.
- If you click behind the in-point, the in-point moves rather than producing an
  impossible range.
- **Deck A / Deck B** chooses `SK/B/1.WAV` or `SK/B/2.WAV`.
- **Send to deck** posts the range; the status line reports the bridge, the
  card check, and then the finished slot.
- Drag the title bar to move it; position, deck and collapsed state persist.
- The URL is never typed: it comes from the page (canonicalised to
  `watch?v=<id>`).

Times are whole seconds because `yt_sample.py` parses `SS`, `MM:SS` or
`HH:MM:SS`. Segments over the 42 s deck limit are refused by the widget, the
bridge and the tool.

## Bridge API

    GET  /health        {"ok":true,"tool_present":true,"card":"/media/...","pending_jobs":0}
    GET  /card          runs `yt_sample.py --check-card`
    POST /sample        {"url","start","end","deck"} -> 202 {"job_id"}
    GET  /jobs/<id>     {"status":"queued|running|done|error","result":{...},"stderr":"..."}

Jobs run one at a time on purpose: two yt-dlp downloads would fight over the
same cache and the same SD slot. A job lives as long as the service.

Bound to 127.0.0.1 only. Cross-origin reads are allowed only from
`chrome-extension://` origins; JSON POSTs only. Set `SPOTTYTUBE_TOKEN` (with
`--token` in the unit) to also require `X-Spottytube-Token`.

## Tests

    python3 -m unittest discover -s tests -p 'test_bridge.py' -v   # 24 bridge tests, stub tool, real HTTP
    python3 -m unittest tests.test_yt_sample -v                    # 3 card-scan tests, real script
    node --test tests/test_selection.mjs                           # 9 state-machine tests

The bridge tests use `tests/stub_tool.py` so they never download or touch a
card. `tests/test_yt_sample.py` covers the card scan against unreadable vfat
mounts (`/recovery`), which used to abort the tool before it found the card.

## Troubleshooting

Bridge down: `systemctl --user status spottytube-bridge` and
`journalctl --user -u spottytube-bridge -n 50`.

Red dot in the widget: the widget cannot reach the bridge. Check the port in
`extension/background.js` (`BASE`) against the unit file.

"No SD card found": `yt_sample.py` refuses before downloading anything. Mount
the card and click Send again, or check `GET /card`.

Widget missing on a video: reload the page once after loading the extension;
content scripts are injected at page load, not retroactively.

"Load unpacked" shows nothing / extension never appears (Flatpak Brave): the
sandbox cannot see `~/Documents`. Run `./install.sh` again (it grants a
read-only override for this folder) and restart Brave.

`PermissionError` on `/recovery` from `yt_sample.py`: fixed in the card scan;
system vfat mounts are skipped instead of aborting the run.

## Layout

    bridge/spottytube_bridge.py    HTTP bridge, stdlib only
    tools/yt_sample.py             vendored sampling CLI (copy of the Spotykach
                                   skill script; keep the two in sync)
    tools/wav_builder.py           its WAV helper module
    extension/manifest.json       MV3, YouTube-only content script
    extension/background.js       all fetches to the bridge happen here
    extension/content.js          the widget (shadow DOM, no page CSS leaks)
    extension/selection.js        start/end state machine (tested in node)
    tests/                        bridge + state-machine tests
