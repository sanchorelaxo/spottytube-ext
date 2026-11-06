#!/usr/bin/env python3
"""Bridge tests: real HTTP against the real handler, stub tool for the CLI.

Run: python3 -m unittest discover -s tests -v
"""

import importlib.util
import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRIDGE_PATH = HERE.parent / "bridge" / "spottytube_bridge.py"
STUB = HERE / "stub_tool.py"

spec = importlib.util.spec_from_file_location("spottytube_bridge", BRIDGE_PATH)
assert spec and spec.loader
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class ServerCase(unittest.TestCase):
    token = ""

    def setUp(self):
        self.args_file = HERE / f"args-{self._testMethodName}.txt"
        os.environ["STUB_ARGS_FILE"] = str(self.args_file)
        os.environ.pop("STUB_FAIL", None)
        os.environ.pop("STUB_SLEEP", None)
        self.server = bridge.make_server("127.0.0.1", 0, STUB, self.token)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.args_file.unlink(missing_ok=True)

    def request(self, path, method="GET", body=None, headers=None, origin=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        if origin:
            request.add_header("Origin", origin)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}"), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}"), dict(error.headers)

    def wait_job(self, job_id, deadline=10.0):
        end = time.time() + deadline
        while time.time() < end:
            status, body, _ = self.request(f"/jobs/{job_id}")
            self.assertEqual(status, 200)
            if body["status"] in ("done", "error"):
                return body
            time.sleep(0.03)
        self.fail("job did not finish")


class TestParsing(unittest.TestCase):
    def test_accepts_ss_mmss_hhmmss(self):
        self.assertEqual(bridge.parse_time("45"), 45.0)
        self.assertEqual(bridge.parse_time("1:30"), 90.0)
        self.assertEqual(bridge.parse_time("1:02:03"), 3723.0)

    def test_rejects_garbage(self):
        for bad in ("", "1.5", "1:2:3:4", "1:75", "-5", "abc"):
            with self.assertRaises(ValueError, msg=bad):
                bridge.parse_time(bad)

    def test_extract_json_skips_preamble(self):
        stdout = 'card: /x (ready)\nnote: hello\n{\n  "deck": "A"\n}\n'
        self.assertEqual(bridge.extract_json(stdout), {"deck": "A"})
        self.assertIsNone(bridge.extract_json("nothing here"))


class TestCardScan(unittest.TestCase):
    """The scan walks real system vfat mounts; one bad probe must not kill it."""

    def test_unreadable_mount_is_skipped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            blocked = Path(tmp) / "recovery"
            (blocked / "SK").mkdir(parents=True)
            os.chmod(blocked, 0o000)
            mounts = Path(tmp) / "mounts"
            mounts.write_text(f"/dev/nvme0n1p2 {blocked} vfat rw 0 0\n")
            try:
                self.assertIsNone(bridge.visible_card(mounts))
            finally:
                os.chmod(blocked, 0o700)

    def test_card_with_sk_folder_is_found(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            card = Path(tmp) / "media" / "SPOTYKACH"
            (card / "SK").mkdir(parents=True)
            mounts = Path(tmp) / "mounts"
            mounts.write_text(f"/dev/sda1 {card} vfat rw 0 0\n")
            self.assertEqual(bridge.visible_card(mounts), str(card))


class TestHealth(ServerCase):
    def test_health_reports_tool(self):
        status, body, _ = self.request("/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["tool_present"])
        self.assertEqual(body["version"], bridge.VERSION)

    def test_card_endpoint(self):
        status, body, _ = self.request("/card")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("state: ready", body["text"])

    def test_card_failure_is_reported_not_raised(self):
        os.environ["STUB_CARD_OK"] = "0"
        try:
            status, body, _ = self.request("/card")
        finally:
            os.environ.pop("STUB_CARD_OK", None)
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertIn("no SD card", body["text"])


class TestSample(ServerCase):
    payload = {"url": "https://www.youtube.com/watch?v=abcdefghijk",
               "start": "1:00", "end": "1:12", "deck": "a"}

    def test_job_runs_tool_and_returns_result(self):
        status, body, _ = self.request("/sample", "POST", self.payload)
        self.assertEqual(status, 202, body)
        self.assertTrue(body["ok"])

        job = self.wait_job(body["job_id"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["result"]["deck"], "A")
        self.assertEqual(job["result"]["tape"], "B")
        self.assertEqual(job["result"]["track"], 1)
        self.assertEqual(job["exit_code"], 0)

    def test_tool_receives_exact_arguments(self):
        status, body, _ = self.request("/sample", "POST", self.payload)
        self.assertEqual(status, 202)
        self.wait_job(body["job_id"])
        argv = self.args_file.read_text().split("\n")
        self.assertEqual(argv, ["--url", self.payload["url"], "--start", "1:00",
                                "--end", "1:12", "--deck", "a", "--json"])

    def test_deck_b_maps_to_second_slot(self):
        payload = dict(self.payload, deck="b")
        status, body, _ = self.request("/sample", "POST", payload)
        self.assertEqual(status, 202)
        job = self.wait_job(body["job_id"])
        self.assertEqual(job["result"]["track"], 2)

    def test_too_long_is_rejected_before_any_download(self):
        payload = dict(self.payload, start="0:00", end="1:30")
        status, body, _ = self.request("/sample", "POST", payload)
        self.assertEqual(status, 400)
        self.assertIn("42s max", body["error"])
        self.assertFalse(self.args_file.exists(), "tool must not run for a bad range")

    def test_end_before_start_is_rejected(self):
        payload = dict(self.payload, start="2:00", end="1:00")
        status, body, _ = self.request("/sample", "POST", payload)
        self.assertEqual(status, 400)
        self.assertIn("must be after start", body["error"])

    def test_bad_deck_is_rejected(self):
        status, body, _ = self.request("/sample", "POST", dict(self.payload, deck="c"))
        self.assertEqual(status, 400)
        self.assertIn("deck", body["error"])

    def test_non_http_url_is_rejected(self):
        for url in ("file:///etc/passwd", "/etc/passwd", "--help"):
            with self.subTest(url=url):
                status, body, _ = self.request("/sample", "POST", dict(self.payload, url=url))
                self.assertEqual(status, 400)

    def test_failure_surfaces_stderr(self):
        os.environ["STUB_FAIL"] = "1"
        status, body, _ = self.request("/sample", "POST", self.payload)
        self.assertEqual(status, 202)
        job = self.wait_job(body["job_id"])
        self.assertEqual(job["status"], "error")
        self.assertFalse(job["ok"])
        self.assertIn("403", job["stderr"])

    def test_unknown_job_is_404(self):
        status, _, _ = self.request("/jobs/deadbeef")
        self.assertEqual(status, 404)

    def test_bad_json_is_400(self):
        request = urllib.request.Request(self.base + "/sample", data=b"not json", method="POST")
        request.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(request, timeout=5)
            self.fail("expected 400")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 400)


class TestCors(ServerCase):
    def test_extension_origin_is_echoed(self):
        status, _, headers = self.request(
            "/health", origin="chrome-extension://abcdefghijklmnop")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"),
                         "chrome-extension://abcdefghijklmnop")

    def test_preflight_advertises_private_network(self):
        status, _, headers = self.request(
            "/sample", "OPTIONS", origin="chrome-extension://abcdefghijklmnop",
            headers={"Access-Control-Request-Method": "POST"})
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Access-Control-Allow-Private-Network"), "true")
        self.assertIn("POST", headers.get("Access-Control-Allow-Methods", ""))

    def test_web_page_origin_is_refused(self):
        status, body, _ = self.request("/sample", "POST", TestSample.payload,
                                       origin="https://evil.example")
        self.assertEqual(status, 403)
        self.assertFalse(body["ok"])


class TestToken(ServerCase):
    token = "s3cret"

    def test_missing_token_is_401(self):
        status, body, _ = self.request("/health")
        self.assertEqual(status, 401)

    def test_wrong_token_is_401(self):
        status, _, _ = self.request("/health", headers={"X-Spottytube-Token": "nope"})
        self.assertEqual(status, 401)

    def test_right_token_passes(self):
        status, body, _ = self.request("/health", headers={"X-Spottytube-Token": "s3cret"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
