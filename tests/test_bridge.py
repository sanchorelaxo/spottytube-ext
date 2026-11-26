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


class TestSettingsPayload(ServerCase):
    """/settings is what the panel renders from: schema plus section hints."""

    def setUp(self):
        super().setUp()
        self._visible_card = bridge.visible_card
        bridge.visible_card = lambda: None

    def tearDown(self):
        bridge.visible_card = self._visible_card
        super().tearDown()

    def test_sections_carry_the_collapse_hints(self):
        status, body, _ = self.request("/settings")
        self.assertEqual(status, 200)
        by_key = {entry["key"]: entry for entry in body["sections"]}
        self.assertEqual(by_key["advanced"]["label"], "Advanced")
        self.assertTrue(by_key["advanced"]["collapsible"])
        self.assertTrue(by_key["advanced"]["collapsed"])
        self.assertFalse(by_key["audio"]["collapsible"])

    def test_every_schema_section_is_announced(self):
        _, body, _ = self.request("/settings")
        announced = {entry["key"] for entry in body["sections"]}
        self.assertTrue({field["section"] for field in body["schema"]} <= announced)
        self.assertEqual([entry["key"] for entry in body["sections"]],
                         [key for key, _ in bridge.app_settings.SECTIONS])

    def test_a_save_that_changes_no_device_value_leaves_the_card_alone(self):
        # The panel posts every field, device keys included and unchanged: that
        # is not a reason to rewrite SK/config.txt, and not a reason to nag.
        _, body, _ = self.request("/settings")
        status, reply, _ = self.request("/settings", method="POST", body=body["settings"])
        self.assertEqual(status, 200)
        self.assertTrue(reply["ok"], msg=reply)
        self.assertNotIn("card_config", reply)

    def test_a_changed_device_value_raises_the_note_once(self):
        _, body, _ = self.request("/settings")
        # The pinned settings file is shared for the whole run, so pick a
        # channel that differs from whatever is stored right now: the test must
        # not depend on the file's prior contents or on test order. Either 3 or
        # 4 also keeps the cross-check happy (both differ from mid_ch_b=2).
        stored = body["settings"]["mid_ch_a"]
        values = dict(body["settings"], mid_ch_a=3 if stored != 3 else 4)
        _, first, _ = self.request("/settings", method="POST", body=values)
        self.assertIn("card_config", first)
        self.assertIn("no card", first["card_config"]["warning"])
        _, second, _ = self.request("/settings", method="POST", body=values)
        self.assertNotIn("card_config", second)

    def _a_different_value(self, key, settings):
        """A value for one device key that differs from the stored value.

        The pinned settings file is shared for the whole run, so read the
        stored value and move away from it: the test must not depend on what an
        earlier test left behind. A MIDI channel also stays clear of the other
        deck's stored channel, since _cross_check rejects the pair being equal;
        a checkbox simply flips.
        """
        field = bridge.app_settings.FIELDS[key]
        current = settings[key]
        if field["control"] == "checkbox":
            return not bool(current)
        other = settings["mid_ch_b" if key == "mid_ch_a" else "mid_ch_a"]
        taken = {int(current), int(other)}
        free = [n for n in range(field["min"], field["max"] + 1) if n not in taken]
        self.assertTrue(free, msg=f"no unused channel left for {key}")
        return free[0]

    def test_every_device_key_is_covered(self):
        """Every CARD_KEYS entry must raise the card note on its own.

        The guard reads the whole CARD_KEYS tuple, so narrowing the key set (to
        its first few, say) would leave the later device keys unable to rewrite
        SK/config.txt at all while these tests stayed green. Move exactly one
        device key per iteration and require the note, then re-post the
        identical payload and require silence. The file is read fresh each pass
        and restored afterwards, so the test neither depends on nor disturbs
        the order it runs in.
        """
        original = bridge.app_settings.load()
        try:
            for key in bridge.app_settings.CARD_KEYS:
                with self.subTest(device_key=key):
                    _, body, _ = self.request("/settings")
                    values = dict(body["settings"])
                    values[key] = self._a_different_value(key, body["settings"])
                    _, first, _ = self.request("/settings", method="POST", body=values)
                    self.assertIn("card_config", first,
                                  msg=f"{key}: a changed value raised no note; {first}")
                    self.assertIn("no card", first["card_config"]["warning"])
                    _, second, _ = self.request("/settings", method="POST", body=values)
                    self.assertNotIn("card_config", second,
                                     msg=f"{key}: re-posting the same value was not quiet; {second}")
        finally:
            bridge.app_settings.save(original)

    def test_an_unchanged_save_leaves_the_card_file_alone(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "SK").mkdir()
            bridge.visible_card = lambda: root
            _, body, _ = self.request("/settings")
            self.request("/settings", method="POST", body=body["settings"])
            self.assertFalse((Path(root) / "SK" / "config.txt").exists(),
                             msg="an unchanged save rewrote the card")

    def test_a_changed_device_value_with_a_card_writes_it(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "SK").mkdir()
            bridge.visible_card = lambda: root
            _, body, _ = self.request("/settings")
            # Same order-independence as above: differ from what is stored now.
            stored = body["settings"]["mid_ch_a"]
            values = dict(body["settings"], mid_ch_a=3 if stored != 3 else 4)
            _, reply, _ = self.request("/settings", method="POST", body=values)
        written = reply["card_config"]["written"]
        self.assertTrue(written, msg=reply)
        self.assertTrue(written.endswith("SK/config.txt"), msg=written)


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
        stdout = 'card: /x (ready)\nnote: hello\n{\n  "tape": "B"\n}\n'
        self.assertEqual(bridge.extract_json(stdout), {"tape": "B"})
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
    # No deck and no mode in the payload: the GUI dropped deck selection
    # entirely, and an omitted mode means "use the settings default", which
    # the tool resolves per call (append unless the user changed it).
    payload = {"url": "https://www.youtube.com/watch?v=abcdefghijk",
               "start": "1:00", "end": "1:12"}

    def test_job_runs_tool_and_returns_result(self):
        status, body, _ = self.request("/sample", "POST", self.payload)
        self.assertEqual(status, 202, body)
        self.assertTrue(body["ok"])

        job = self.wait_job(body["job_id"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["result"]["tape"], "B")
        self.assertEqual(job["result"]["slot"], 1)
        self.assertEqual(job["result"]["mode"], "append")
        self.assertEqual(job["exit_code"], 0)

    def test_tool_receives_exact_arguments(self):
        status, body, _ = self.request("/sample", "POST", self.payload)
        self.assertEqual(status, 202)
        self.wait_job(body["job_id"])
        argv = self.args_file.read_text().split("\n")
        self.assertEqual(argv, ["--url", self.payload["url"], "--start", "1:00",
                                "--end", "1:12", "--json"])

    # -- save mode ----------------------------------------------------------

    def test_default_mode_is_append_and_passes_no_flag(self):
        status, body, _ = self.request("/sample", "POST", self.payload)
        self.assertEqual(status, 202, body)
        self.wait_job(body["job_id"])
        self.assertEqual(body["meta"]["mode"], "append")
        self.assertNotIn("--mode", self.args_file.read_text().split("\n"))

    def test_append_mode_reaches_the_tool(self):
        status, body, _ = self.request("/sample", "POST",
                                       dict(self.payload, mode="append"))
        self.assertEqual(status, 202, body)
        job = self.wait_job(body["job_id"])
        self.assertEqual(job["result"]["mode"], "append")
        argv = self.args_file.read_text().split("\n")
        self.assertEqual(argv[argv.index("--mode") + 1], "append")
        self.assertEqual(body["meta"]["mode"], "append")

    def test_bad_mode_is_rejected(self):
        status, body, _ = self.request("/sample", "POST",
                                       dict(self.payload, mode="sideways"))
        self.assertEqual(status, 400)
        self.assertIn("mode", body["error"])

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

    # -- playback speed ---------------------------------------------------

    def test_speed_reaches_the_tool(self):
        status, body, _ = self.request("/sample", "POST", dict(self.payload, speed=1.5))
        self.assertEqual(status, 202, body)
        self.wait_job(body["job_id"])
        argv = self.args_file.read_text().split("\n")
        self.assertIn("--speed", argv)
        self.assertEqual(argv[argv.index("--speed") + 1], "1.5")
        self.assertEqual(body["meta"]["speed"], 1.5)

    def test_absent_speed_means_normal_and_no_flag(self):
        status, body, _ = self.request("/sample", "POST", self.payload)
        self.assertEqual(status, 202)
        self.wait_job(body["job_id"])
        self.assertNotIn("--speed", self.args_file.read_text().split("\n"))
        self.assertEqual(body["meta"]["speed"], 1)

    def test_a_whole_number_speed_is_written_without_a_trailing_zero(self):
        status, body, _ = self.request("/sample", "POST", dict(self.payload, speed=2))
        self.assertEqual(status, 202)
        self.wait_job(body["job_id"])
        argv = self.args_file.read_text().split("\n")
        self.assertEqual(argv[argv.index("--speed") + 1], "2")

    def test_speed_outside_the_player_range_is_rejected(self):
        for bad in (0, -1, 0.1, 2.5, "abc", "fast", True):
            with self.subTest(bad=bad):
                status, body, _ = self.request(
                    "/sample", "POST", dict(self.payload, speed=bad))
                self.assertEqual(status, 400, body)
                self.assertIn("speed", body["error"])
        self.assertFalse(self.args_file.exists(), "tool must not run for a bad speed")

    def test_a_speed_that_would_overrun_the_deck_is_rejected(self):
        payload = dict(self.payload, start="0:00", end="0:30", speed=0.5)
        status, body, _ = self.request("/sample", "POST", payload)
        self.assertEqual(status, 400)
        self.assertIn("42", body["error"])
        self.assertFalse(self.args_file.exists())

    def test_a_fast_range_over_42_source_seconds_is_accepted(self):
        # 42s heard at 1.5x moves the source 63s; the deck stores 42s.
        payload = dict(self.payload, start="1:00", end="2:03", speed=1.5)
        status, body, _ = self.request("/sample", "POST", payload)
        self.assertEqual(status, 202, body)

    def test_the_same_range_at_normal_speed_is_rejected(self):
        payload = dict(self.payload, start="1:00", end="2:03")
        status, body, _ = self.request("/sample", "POST", payload)
        self.assertEqual(status, 400)
        self.assertIn("42s max", body["error"])
        self.assertFalse(self.args_file.exists())


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


class TestRestart(ServerCase):
    """POST /restart: answer first, then exit non-zero so systemd brings it back.

    The installed unit runs Restart=on-failure, RestartSec=2, so a non-zero
    exit IS the restart mechanism — no systemctl child process to outlive the
    cgroup kill. Under systemd a service process carries INVOCATION_ID; its
    absence means a manual run, where exiting would just kill the bridge.
    """

    def setUp(self):
        super().setUp()
        os.environ.pop("INVOCATION_ID", None)

    def tearDown(self):
        os.environ.pop("INVOCATION_ID", None)
        super().tearDown()

    def test_under_systemd_is_true_only_with_an_invocation_id(self):
        self.assertTrue(bridge.under_systemd({"INVOCATION_ID": "abc"}))
        self.assertFalse(bridge.under_systemd({}))

    def test_the_restart_exit_code_is_nonzero_so_on_failure_picks_it_up(self):
        self.assertNotEqual(bridge.EXIT_RESTART, 0)

    def test_schedule_restart_calls_exit_with_the_restart_code(self):
        calls = []
        bridge.schedule_restart(exit_fn=calls.append, delay=0.01)
        time.sleep(0.3)
        self.assertEqual(calls, [bridge.EXIT_RESTART])

    def test_refused_when_not_running_under_systemd(self):
        status, body, _ = self.request("/restart", "POST")
        self.assertEqual(status, 409)
        self.assertFalse(body["ok"])
        self.assertIn("systemd", body["error"])
        status, body, _ = self.request("/health")
        self.assertEqual(status, 200, "the server must survive a refused restart")

    def test_under_systemd_it_answers_ok_then_schedules_the_exit(self):
        os.environ["INVOCATION_ID"] = "test-invocation"
        scheduled = []
        original = bridge.schedule_restart
        bridge.schedule_restart = lambda *a, **kw: scheduled.append((a, kw))
        try:
            status, body, _ = self.request("/restart", "POST")
        finally:
            bridge.schedule_restart = original
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["restarting"])
        self.assertEqual(len(scheduled), 1)

    def test_health_reports_the_version_so_the_widget_can_show_it(self):
        status, body, _ = self.request("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], bridge.VERSION)


if __name__ == "__main__":
    unittest.main(verbosity=2)
