#!/usr/bin/env bash
# SpottyTube installer: link the bridge, install a user service, start it.
#
#   ./install.sh                 install and start
#   ./install.sh --port 8766     use another port (edit manifest too)
#   ./install.sh --no-service    only link the binary
#   ./install.sh --uninstall     stop and remove everything
#
# Idempotent: re-running rewrites the unit and restarts the service.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/spottytube-bridge.service"
LINK="$BIN_DIR/spottytube-bridge"
PORT="${SPOTTYTUBE_PORT:-8765}"
DO_SERVICE=1
DO_UNINSTALL=0

while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="${2:?--port needs a value}"; shift 2 ;;
        --no-service) DO_SERVICE=0; shift ;;
        --uninstall) DO_UNINSTALL=1; shift ;;
        -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

if [ "$DO_UNINSTALL" = 1 ]; then
    systemctl --user disable --now spottytube-bridge.service 2>/dev/null || true
    rm -f "$UNIT" "$LINK"
    systemctl --user daemon-reload 2>/dev/null || true
    echo "removed: $UNIT, $LINK"
    echo "the extension itself is still loaded in the browser — remove it at brave://extensions"
    exit 0
fi

echo "spottytube root: $ROOT"
mkdir -p "$BIN_DIR"

chmod +x "$ROOT/bridge/spottytube_bridge.py"
ln -sfn "$ROOT/bridge/spottytube_bridge.py" "$LINK"
echo "linked  $LINK"

chmod +x "$ROOT/tools/yt_sample.py"
if [ ! -f "$ROOT/tools/yt_sample.py" ] || [ ! -f "$ROOT/tools/wav_builder.py" ]; then
    echo "error: vendored tool missing (need tools/yt_sample.py and tools/wav_builder.py)" >&2
    exit 1
fi
echo "tool    $ROOT/tools/yt_sample.py (vendored, no ~/.local/bin needed)"

if [ ! -f "$ROOT/bridge/spottytube_bridge.py" ]; then
    echo "error: bridge script missing under $ROOT/bridge" >&2
    exit 1
fi

# Flatpak Brave runs in a sandbox that only sees xdg-download, xdg-desktop and
# /tmp by default, so "Load unpacked" from ~/Documents fails with no error at
# all. Grant read-only access to this folder; harmless when Brave is not Flatpak.
if flatpak info com.brave.Browser >/dev/null 2>&1; then
    if flatpak info --show-permissions com.brave.Browser 2>/dev/null | grep -qF "$ROOT"; then
        echo "flatpak: brave already sees $ROOT"
    else
        flatpak override --user --filesystem="$ROOT:ro" com.brave.Browser
        echo "flatpak: granted brave read-only access to $ROOT (restart brave to apply)"
    fi
fi

if [ "$DO_SERVICE" = 1 ]; then
    mkdir -p "$UNIT_DIR"
    cat > "$UNIT" <<UNIT_EOF
[Unit]
Description=SpottyTube local bridge (runs yt_sample.py for the browser extension)
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

Load the extension (Brave or Chrome):
  1. open brave://extensions  (chrome://extensions)
  2. turn on Developer mode
  3. Load unpacked -> $ROOT/extension
  4. open any YouTube video; the SpottyTube panel sits bottom right

Service: systemctl --user status spottytube-bridge
Logs:    journalctl --user -u spottytube-bridge -f
Tests:   python3 -m unittest discover -s tests -v && node --test tests/test_selection.mjs
EOF
