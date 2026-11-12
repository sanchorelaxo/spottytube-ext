#!/usr/bin/env python3
"""SpottyTube local bridge — lets a browser extension run the sampling tool.

A browser extension cannot start a process. This listens on 127.0.0.1 and turns
one small JSON request into one ``sample_tool.py`` run, so the widget on a page
can hand a time range to the Spotykach.

    POST /sample   {"url","start","end"}  -> 202 {"job_id"}
                   optional "mode" ("bump"|"append"), "speed", "source",
                   "transport", "tape"
    GET  /jobs/<id>                              -> job state, result, output
    GET  /status                                 -> which transports work now
    GET  /card                                   -> `sample_tool.py --check-card`
    GET  /health                                 -> tool present, card visible

``source`` is a string option, not an assumption: the request may say
``"source": "youtube"`` to have the URL's video id parsed, but the default is
generic handling of whatever URL was given. ``transport`` (``auto``/``usb``/
``card``) and ``tape`` pass through the same way.

Runs are serialised by one worker thread on purpose: downloads are heavy and two
concurrent cuts would fight over the same cache and the same tape.

Security posture: binds to 127.0.0.1 only, accepts JSON POSTs only, and answers
CORS preflights only for chrome-extension:// origins (Brave and Chrome both use
that scheme). Set SPOTTYTUBE_TOKEN to also require an X-Spottytube-Token header.

Stdlib only, no dependencies.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

VERSION = "0.2.0"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
# The tool ships with the project (tools/sample_tool.py + tools/wav_builder.py
# + tools/sk_usb.py + tools/sk_install.py), resolved from this file rather than
# from ~/.local/bin, so the checkout is self-contained. --tool / SPOTTYTUBE_TOOL
# still override it.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TOOL = PROJECT_ROOT / "tools" / "sample_tool.py"

# The settings schema is the single source of truth shared with the tool and
# the widget; wav_builder writes SK/config.txt for the device-scope keys.
sys.path.insert(0, str(PROJECT_ROOT / "tools"))
import settings as app_settings  # noqa: E402
import wav_builder as wav_builder_lib  # noqa: E402

# Mirrors sample_tool.MAX_LOOP_SECONDS. Checked here so an impossible range
# costs a 400 instead of a download.
MAX_LOOP_SECONDS = 42.0
# Mirrors sample_tool.MIN_SPEED / MAX_SPEED: the rates a player offers. A
# slowed-down sample is longer than the range, so the ceiling depends on it.
MIN_SPEED = 0.25
MAX_SPEED = 2.0
JOB_TIMEOUT_SECONDS = 900
MAX_BODY_BYTES = 64 * 1024


# --- time helpers (mirror of sample_tool.parse_time) ------------------------


def parse_time(value):
    """Parse ``SS``, ``MM:SS`` or ``HH:MM:SS`` into seconds (float)."""
    if isinstance(value, bool):
        raise ValueError("invalid time")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError("invalid time: negative")
        return float(value)

    text = str(value).strip()
    if not text:
        raise ValueError("invalid time: empty")

    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"invalid time {text!r}: too many fields")
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        raise ValueError(f"invalid time {text!r}") from None
    if any(n < 0 for n in numbers):
        raise ValueError(f"invalid time {text!r}: negative")
    if any(n > 59 for n in numbers[1:]):
        raise ValueError(f"invalid time {text!r}: field above 59")
    if len(numbers) == 1:
        return float(numbers[0])
    if len(numbers) == 2:
        minutes, seconds = numbers
        return float(minutes * 60 + seconds)
    hours, minutes, seconds = numbers
    return float(hours * 3600 + minutes * 60 + seconds)


def visible_card(mounts_path="/proc/mounts"):
    """First mounted vfat/exfat mount point that holds an SK/ folder.

    Every probe is guarded: a system vfat mount such as /recovery can be
    unreadable, and one PermissionError must not kill the scan (or the daemon).
    """
    try:
        lines = Path(mounts_path).read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        if len(fields) < 3 or fields[2] not in ("vfat", "exfat", "msdos", "fat"):
            continue
        root = Path(fields[1].replace("\\040", " "))
        try:
            if (root / "SK").is_dir():
                return str(root)
        except OSError:
            continue
    return None


def extract_json(stdout):
    """Pull the result object out of the tool's --json output.

    The CLI prints a human line ("card: ... (ready)") before the JSON, so the
    payload starts at the first brace.
    """
    index = stdout.find("{")
    if index < 0:
        return None
    try:
        return json.loads(stdout[index:])
    except ValueError:
        return None


# --- job store ------------------------------------------------------------


class JobStore:
    """One worker thread, many jobs, results kept until the process exits."""

    def __init__(self, tool, python=None, timeout=JOB_TIMEOUT_SECONDS):
        self.tool = Path(tool)
        self.python = python or sys.executable
        self.timeout = timeout
        self._jobs = {}
        self._lock = threading.Lock()
        self._queue = queue.Queue()
        worker = threading.Thread(target=self._run_forever, daemon=True,
                                  name="spottytube-worker")
        worker.start()

    def submit(self, argv, meta):
        job_id = secrets.token_hex(4)
        with self._lock:
            self._jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "created": time.time(),
                "started": None,
                "finished": None,
                "meta": meta,
                "result": None,
                "stdout": "",
                "stderr": "",
                "exit_code": None,
            }
        self._queue.put((job_id, argv))
        return job_id

    def get(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def pending(self):
        with self._lock:
            return sum(1 for j in self._jobs.values()
                       if j["status"] in ("queued", "running"))

    def _update(self, job_id, **fields):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.update(fields)

    def _run_forever(self):
        while True:
            job_id, argv = self._queue.get()
            self._update(job_id, status="running", started=time.time())
            try:
                proc = subprocess.run(argv, capture_output=True, text=True,
                                      timeout=self.timeout)
                stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
            except subprocess.TimeoutExpired:
                stdout, stderr, code = "", (
                    f"sampling tool timed out after {self.timeout}s"), 124
            except OSError as error:
                stdout, stderr, code = "", f"could not start the sampling tool: {error}", 127

            result = extract_json(stdout)
            ok = code == 0 and result is not None
            self._update(
                job_id,
                status="done" if ok else "error",
                stdout=stdout[-4000:],
                stderr=stderr[-4000:],
                exit_code=code,
                result=result,
                finished=time.time(),
            )


# --- HTTP -----------------------------------------------------------------


def _origin_allowed(origin):
    """Only pages served by an extension may use the bridge."""
    if not origin:
        return True  # curl / local script; useful for testing and the CLI
    return origin.startswith("chrome-extension://")


def _validate(body):
    """Return (payload, error_message)."""
    if not isinstance(body, dict):
        return None, "expected a JSON object"

    url = str(body.get("url", "")).strip()
    start = str(body.get("start", "")).strip()
    end = str(body.get("end", "")).strip()
    # Placement on the chain: bump (slot 1, everything shifts down) or append
    # (first free slot, nothing moves). Omitted means "whatever the settings
    # file says" — the tool resolves default_mode per call, so a settings save
    # takes effect on the next save without touching the widget.
    mode = str(body.get("mode", "") or "").strip().lower() or None
    # Optional pass-throughs. `source` stays a plain string so the bridge never
    # hardcodes a provider; the tool decides what it means and rejects unknowns.
    source = str(body.get("source", "") or "").strip().lower() or None
    transport = str(body.get("transport", "") or "").strip().lower() or None
    tape = str(body.get("tape", "") or "").strip().upper() or None
    raw_speed = body.get("speed", 1.0)

    if not url.startswith(("http://", "https://")):
        return None, "url must be an http(s) URL"
    if mode is not None and mode not in ("bump", "append"):
        return None, "mode must be 'bump' or 'append'"
    if transport is not None and transport not in ("auto", "usb", "card"):
        return None, "transport must be 'auto', 'usb' or 'card'"

    try:
        if isinstance(raw_speed, bool):
            raise ValueError
        speed = float(1.0 if raw_speed is None else raw_speed)
        if not math.isfinite(speed):
            raise ValueError
    except (TypeError, ValueError):
        return None, (f"speed must be a number between {MIN_SPEED:g} and "
                      f"{MAX_SPEED:g}")
    if not (MIN_SPEED <= speed <= MAX_SPEED):
        return None, (f"speed {speed:g} is outside {MIN_SPEED:g}x-{MAX_SPEED:g}x")

    for name, value in (("start", start), ("end", end)):
        if not value:
            return None, f"{name} is required"

    try:
        start_s, end_s = parse_time(start), parse_time(end)
    except ValueError as error:
        return None, str(error)
    if end_s <= start_s:
        return None, f"end ({end_s:g}s) must be after start ({start_s:g}s)"
    # The deck limit is on what gets stored: the range divided by the speed it
    # was heard at. 63 s of source at 1.5x is a 42 s sample and is accepted.
    plays = (end_s - start_s) / speed
    if plays > MAX_LOOP_SECONDS:
        return None, (f"{end_s - start_s:g}s at {speed:g}x plays for "
                      f"{plays:.2f}s; a deck holds {MAX_LOOP_SECONDS:g}s max")

    return {
        "url": url, "start": start, "end": end, "mode": mode,
        "source": source, "transport": transport, "tape": tape, "speed": speed,
    }, None


# -- service restart ---------------------------------------------------------
# The systemd unit runs Restart=on-failure, RestartSec=2, so a controlled
# non-zero exit IS the restart: no systemctl child process is spawned (one
# would share the unit's cgroup and be killed by the very restart it tries
# to trigger). The delay lets the HTTP response flush before the process dies.
EXIT_RESTART = 70          # EX_SOFTWARE: non-zero, so on-failure restarts
RESTART_DELAY = 0.5        # seconds between the response and the exit


def under_systemd(env=None):
    """True when this process was started by systemd (services get INVOCATION_ID)."""
    return bool((os.environ if env is None else env).get("INVOCATION_ID"))


def schedule_restart(exit_fn=None, delay=RESTART_DELAY):
    """Exit with EXIT_RESTART after ``delay`` seconds; systemd brings us back."""
    exit_fn = exit_fn or os._exit
    timer = threading.Timer(delay, exit_fn, args=(EXIT_RESTART,))
    timer.daemon = True
    timer.start()


class Handler(BaseHTTPRequestHandler):
    server_version = f"SpottyTube/{VERSION}"
    protocol_version = "HTTP/1.1"

    @property
    def bridge(self) -> "BridgeServer":
        return cast("BridgeServer", self.server)

    # -- plumbing ---------------------------------------------------------

    def log_message(self, format, *args):
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), format % args))
        sys.stderr.flush()

    def _cors(self, origin):
        if origin and _origin_allowed(origin) and origin != "null":
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Spottytube-Token")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")

    def _send(self, status, payload):
        body = json.dumps(payload, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors(self.headers.get("Origin"))
        self.end_headers()
        self.wfile.write(body)

    def _denied(self):
        token = self.bridge.token
        return bool(token) and self.headers.get("X-Spottytube-Token") != token

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return None, "empty body"
        if length > MAX_BODY_BYTES:
            return None, "body too large"
        raw = self.rfile.read(length)
        try:
            return json.loads(raw), None
        except ValueError:
            return None, "body is not valid JSON"

    # -- routes -----------------------------------------------------------

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors(self.headers.get("Origin"))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if _origin_allowed(self.headers.get("Origin")) is False:
            self._send(403, {"ok": False, "error": "origin not allowed"})
            return
        if self._denied():
            self._send(401, {"ok": False, "error": "missing or wrong token"})
            return

        path = self.path.split("?")[0].rstrip("/")

        if path in ("", "/health"):
            tool = self.bridge.store.tool
            self._send(200, {
                "ok": True,
                "name": "spottytube-bridge",
                "version": VERSION,
                "tool": str(tool),
                "tool_present": tool.is_file(),
                "card": visible_card(),
                "pending_jobs": self.bridge.store.pending(),
            })
            return

        if path == "/status":
            # One call for the widget's health colour: the tool reports whether
            # USB or the card can take a sample right now.
            argv = [self.bridge.store.python, str(self.bridge.store.tool),
                    "--status", "--json"]
            try:
                proc = subprocess.run(argv, capture_output=True, text=True, timeout=25)
                report = extract_json(proc.stdout) or {}
                self._send(200, {
                    "ok": True,
                    "ready": bool(report.get("ready")),
                    "transport": report.get("transport"),
                    "usb": report.get("usb", {}),
                    "card": report.get("card", {}),
                    "card_visible": visible_card(),
                    "stderr": proc.stderr.strip()[-500:],
                })
            except (OSError, subprocess.SubprocessError) as error:
                self._send(200, {"ok": False, "ready": False, "transport": None,
                                 "error": str(error), "card_visible": visible_card()})
            return

        if path == "/card":
            argv = [self.bridge.store.python, str(self.bridge.store.tool), "--check-card"]
            try:
                proc = subprocess.run(argv, capture_output=True, text=True, timeout=25)
                text = (proc.stdout + proc.stderr).strip()
                self._send(200, {"ok": proc.returncode == 0, "text": text,
                                 "card": visible_card()})
            except (OSError, subprocess.SubprocessError) as error:
                self._send(500, {"ok": False, "error": str(error)})
            return

        if path.startswith("/jobs/"):
            job = self.bridge.store.get(path[len("/jobs/"):])
            if job is None:
                self._send(404, {"ok": False, "error": "unknown job"})
                return
            self._send(200, {"ok": job["status"] == "done", **job})
            return

        if path == "/settings":
            # Schema + values: the widget renders the panel straight from this,
            # so labels, controls, ranges and hover help all come from one file.
            self._send(200, {"ok": True,
                             "settings": app_settings.load(),
                             "schema": app_settings.schema_json(),
                             "sections": [list(s) for s in app_settings.SECTIONS]})
            return

        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if _origin_allowed(self.headers.get("Origin")) is False:
            self._send(403, {"ok": False, "error": "origin not allowed"})
            return
        if self._denied():
            self._send(401, {"ok": False, "error": "missing or wrong token"})
            return
        route = self.path.split("?")[0].rstrip("/")
        if route == "/settings":
            self._post_settings()
            return
        if route == "/restart":
            self._post_restart()
            return
        if route != "/sample":
            self._send(404, {"ok": False, "error": "not found"})
            return

        body, error = self._read_json()
        if error:
            self._send(400, {"ok": False, "error": error})
            return

        payload, error = _validate(body)
        if error or payload is None:
            self._send(400, {"ok": False, "error": error or "invalid request"})
            return

        store = self.bridge.store
        if not store.tool.is_file():
            self._send(500, {"ok": False,
                             "error": f"sampling tool not found at {store.tool}"})
            return

        argv = [store.python, str(store.tool),
                "--url", payload["url"],
                "--start", payload["start"],
                "--end", payload["end"],
                "--json"]
        # The tool resolves omitted values (mode, tape, transport...) from the
        # settings file per call, so --mode is only forwarded when the caller
        # explicitly picked one.
        if payload.get("mode"):
            argv += ["--mode", payload["mode"]]
        for flag, key in (("--source", "source"),
                          ("--transport", "transport"),
                          ("--tape", "tape")):
            if payload.get(key):
                argv += [flag, payload[key]]
        # 1x is the tool's default, so the flag only appears when it differs.
        if payload["speed"] != 1.0:
            argv += ["--speed", f"{payload['speed']:g}"]
        meta = dict(payload)
        if meta.get("mode") is None:
            meta["mode"] = app_settings.load()["default_mode"]
        job_id = store.submit(argv, meta)
        self._send(202, {"ok": True, "job_id": job_id, "meta": meta})

    def _post_settings(self):
        """Validate, persist and hot-apply posted settings. No restart.

        The token check in ``do_POST`` already ran. The posted object may be
        partial: every field not mentioned keeps its current value, so the
        widget can save a single section.
        """
        body, error = self._read_json()
        if error:
            self._send(400, {"ok": False, "error": error})
            return
        merged, errors = app_settings.validate(body)
        if errors:
            self._send(400, {"ok": False, "error": "; ".join(errors)})
            return
        tool_path = merged["tool_path"]
        if tool_path and not Path(tool_path).is_file():
            self._send(400, {"ok": False, "error":
                             f"tool_path {tool_path!r} is not an existing file"})
            return
        app_settings.save(merged)
        apply_runtime_settings(self.bridge, merged)

        card_config = None
        if any(key in body for key in app_settings.CARD_KEYS):
            card_config = write_card_config(merged)
        self._send(200, {"ok": True, "settings": app_settings.load(),
                         "card_config": card_config, "restart_needed": False})

    def _post_restart(self):
        """Restart the bridge service: answer, then exit non-zero.

        Only safe under systemd, whose ``Restart=on-failure`` turns the exit
        into a fresh process running the current code on disk — the fix for
        the "widget runs new code against an old server" trap. A manually
        started bridge stays up and gets a 409 explaining why.
        """
        if not under_systemd():
            self._send(409, {"ok": False, "error":
                             "bridge is not running under systemd — "
                             "restart it manually"})
            return
        self._send(200, {"ok": True, "restarting": True, "version": VERSION})
        schedule_restart()


def apply_runtime_settings(bridge, cfg):
    """Hot-apply the bridge-scope keys to a running server.

    ``tool_path`` swaps the script the job store shells out to on the *next*
    submit; ``usb_port`` is exported so the next tool subprocess picks it up
    (sk_usb reads SPOTTY_USB_PORT per call). Nothing restarts.
    """
    tool = (cfg.get("tool_path") or "").strip()
    bridge.store.tool = Path(tool) if tool else Path(DEFAULT_TOOL)
    port = (cfg.get("usb_port") or "").strip()
    if port:
        os.environ["SPOTTY_USB_PORT"] = port
    else:
        os.environ.pop("SPOTTY_USB_PORT", None)


def write_card_config(cfg):
    """Write the device-scope keys to SK/config.txt on a mounted card.

    Returns a status dict for the response. When no card is mounted the
    settings are already persisted — the answer says so instead of failing;
    re-saving with a card present completes the write.
    """
    root = visible_card()
    if not root:
        return {"written": None,
                "warning": "no card mounted — device settings are saved; "
                           "insert the card and save again to write "
                           "SK/config.txt"}
    values = {key: int(cfg[key]) for key in app_settings.CARD_KEYS}
    try:
        written = wav_builder_lib.write_config(values, Path(root) / "SK")
        return {"written": str(written)}
    except (OSError, ValueError) as error:
        return {"written": None, "error": str(error)}


class BridgeServer(ThreadingHTTPServer):
    """ThreadingHTTPServer carrying the token and the job store."""

    daemon_threads = True
    token: str
    store: JobStore

    def __init__(self, address, token="", tool=DEFAULT_TOOL, python=None):
        super().__init__(address, Handler)
        self.token = token
        self.store = JobStore(tool, python=python)


def make_server(host=DEFAULT_HOST, port=DEFAULT_PORT, tool=DEFAULT_TOOL,
                token="", python=None):
    """Build the server without serving it; tests pass port=0."""
    return BridgeServer((host, port), token=token, tool=tool, python=python)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="spottytube-bridge",
        description="Local HTTP bridge that runs sample_tool.py for the SpottyTube extension.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=int(os.environ.get("SPOTTYTUBE_PORT", DEFAULT_PORT)))
    parser.add_argument("--tool", default=os.environ.get("SPOTTYTUBE_TOOL", str(DEFAULT_TOOL)))
    parser.add_argument("--token", default=os.environ.get("SPOTTYTUBE_TOKEN", ""),
                        help="require X-Spottytube-Token on every request")
    args = parser.parse_args(argv)

    server = make_server(args.host, args.port, args.tool, args.token)
    host, port = server.server_address[:2]
    tool = Path(args.tool)
    print(f"spottytube-bridge {VERSION} on http://{host}:{port}", file=sys.stderr)
    print(f"  tool: {tool} ({'found' if tool.is_file() else 'MISSING'})", file=sys.stderr)
    print(f"  card: {visible_card() or 'not detected'}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping", file=sys.stderr)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())