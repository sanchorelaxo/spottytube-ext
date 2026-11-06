#!/usr/bin/env bash
# SpottyTube installer: fetch deps, link the bridge, install a user service, start it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/spottytube-bridge.service"
LINK="$BIN_DIR/spottytube-bridge"
PORT="${SPOTTYTUBE_PORT:-8765}"
BROWSER_SPEC="auto"
DO_SERVICE=1
DO_UNINSTALL=0

# shellcheck source=installers/browsers.sh
. "$ROOT/installers/browsers.sh"

usage() {
    cat <<'TXT'
SpottyTube installer: fetch deps, link the bridge, install a user service, start it.

  ./install.sh                     install and start (every browser found)
  ./install.sh --browser chrome    only Chrome; brave, chromium, or a comma
                                   list such as "chrome,brave" also work
  ./install.sh --port 8766         use another port (edit manifest too)
  ./install.sh --no-service        only link the binary
  ./install.sh --uninstall         stop and remove everything

Idempotent: re-running rewrites the unit and restarts the service.
TXT
}

while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="${2:?--port needs a value}"; shift 2 ;;
        --browser) BROWSER_SPEC="${2:?--browser needs a value}"; shift 2 ;;
        --no-service) DO_SERVICE=0; shift ;;
        --uninstall) DO_UNINSTALL=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

# Validate the browser choice before anything is written or started: a typo must
# cost nothing.
SELECTED="$(filter_browsers "$BROWSER_SPEC")" || exit 2
if ! has_browsers "$BROWSER_SPEC" && [ "$BROWSER_SPEC" != auto ] && [ "$BROWSER_SPEC" != all ]; then
    echo "warning: no $BROWSER_SPEC browser on PATH; installing the bridge only" >&2
    echo "         the extension still loads, the steps are printed at the end" >&2
fi

if [ "$DO_UNINSTALL" = 1 ]; then
    systemctl --user disable --now spottytube-bridge.service 2>/dev/null || true
    rm -f "$UNIT" "$LINK"
    systemctl --user daemon-reload 2>/dev/null || true
    echo "removed: $UNIT, $LINK"
    echo "the extension itself is still loaded in the browser — remove it at:"
    if [ -n "$SELECTED" ]; then
        echo "$SELECTED" | while IFS=$'\t' read -r family _ label; do
            echo "  $label: $(browser_url "$family")"
        done
    else
        echo "  chrome://extensions (Chrome, Chromium) or brave://extensions (Brave)"
    fi
    exit 0
fi

echo "spottytube root: $ROOT"
mkdir -p "$BIN_DIR"

chmod +x "$ROOT/bridge/spottytube_bridge.py"
ln -sfn "$ROOT/bridge/spottytube_bridge.py" "$LINK"
echo "linked  $LINK"

chmod +x "$ROOT/tools/sample_tool.py" "$ROOT/tools/fetch_wav_builder.py"
if [ ! -f "$ROOT/tools/sample_tool.py" ]; then
    echo "error: sampling tool missing at $ROOT/tools/sample_tool.py" >&2
    exit 1
fi
echo "tool    $ROOT/tools/sample_tool.py"

# wav_builder.py is not vendored here: it lives in the Spotykach skill repo and
# is fetched so there is only ever one copy to keep correct. The fetcher checks
# the API this project uses before replacing anything, so a bad download cannot
# break a working install. Override with WAV_BUILDER_URL for an offline clone.
echo "dep     fetching wav_builder.py"
WB_ARGS=(--dest "$ROOT/tools/wav_builder.py")
if [ -n "${WAV_BUILDER_URL:-}" ]; then
    WB_ARGS+=(--url "$WAV_BUILDER_URL")
fi
if ! python3 "$ROOT/tools/fetch_wav_builder.py" "${WB_ARGS[@]}"; then
    echo "error: could not fetch wav_builder.py (see above); sampling cannot run without it" >&2
    exit 1
fi

if [ ! -f "$ROOT/bridge/spottytube_bridge.py" ]; then
    echo "error: bridge script missing under $ROOT/bridge" >&2
    exit 1
fi

# Flatpak browsers run in a sandbox that only sees xdg-download, xdg-desktop and
# /tmp by default, so "Load unpacked" from ~/Documents fails with no error at
# all. Grant read-only access to this folder for every Flatpak browser the user
# asked for; harmless when a browser is not Flatpak or not installed.
if [ -n "$SELECTED" ]; then
    while IFS=$'\t' read -r family kind label; do
        [ "$kind" = flatpak ] || continue
        APP="$(flatpak_app "$family")"
        if flatpak info --show-permissions "$APP" 2>/dev/null | grep -qF "$ROOT"; then
            echo "flatpak: $label already sees $ROOT"
        else
            flatpak override --user --filesystem="$ROOT:ro" "$APP"
            echo "flatpak: granted $label read-only access to $ROOT (restart $label to apply)"
        fi
    done <<< "$SELECTED"
fi

if [ "$DO_SERVICE" = 1 ]; then
    mkdir -p "$UNIT_DIR"
    cat > "$UNIT" <<UNIT_EOF
[Unit]
Description=SpottyTube local bridge (runs sample_tool.py for the browser extension)
After=network.target

[Service]
Type=simple
ExecStart=%h/.local/bin/spottytube-bridge --port $PORT
Restart=on-failure
RestartSec=2
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
UNIT_EOF
    echo "wrote   $UNIT"

    systemctl --user daemon-reload
    systemctl --user enable spottytube-bridge.service >/dev/null
    systemctl --user restart spottytube-bridge.service
    echo "started spottytube-bridge on port $PORT"

    HEALTHY=0
    for _ in $(seq 1 40); do
        if python3 - "$PORT" <<'PY' >/dev/null 2>&1
import json, sys, urllib.request
with urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/health", timeout=1) as r:
    sys.exit(0 if json.load(r)["ok"] else 1)
PY
        then
            echo "health: bridge answering"
            HEALTHY=1
            break
        fi
        sleep 0.25
    done
    if [ "$HEALTHY" = 0 ]; then
        echo "warning: bridge did not answer on port $PORT" >&2
        systemctl --user status spottytube-bridge.service --no-pager 2>&1 | tail -5 >&2 || true
        echo "  see: journalctl --user -u spottytube-bridge -n 40" >&2
    fi
fi

cat <<EOF

Load the extension (Chrome, Brave and Chromium all work):
EOF
if [ -n "$SELECTED" ]; then
    while IFS=$'\t' read -r family _ label; do
        printf '  %s\n    1. open %s\n    2. turn on Developer mode\n    3. Load unpacked -> %s/extension\n' \
            "$label" "$(browser_url "$family")" "$ROOT"
    done <<< "$SELECTED"
else
    cat <<EOF
  no Chrome, Brave or Chromium was found on PATH; in the browser you use
    1. open chrome://extensions (Brave: brave://extensions)
    2. turn on Developer mode
    3. Load unpacked -> $ROOT/extension
EOF
fi

cat <<EOF

Then open any video page; the SpottyTube panel sits bottom right.

Service: systemctl --user status spottytube-bridge
Logs:    journalctl --user -u spottytube-bridge -f
Tests:   python3 -m unittest discover -s tests -v && node --test tests/test_selection.mjs
EOF
