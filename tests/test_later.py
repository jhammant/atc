"""Schedules and owner plug-ins, from transcripts through the shared views and transports."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

import fixture
from test_units import load_atc
from test_web import HTTP, ORIGIN, Passkey


class Cron(unittest.TestCase):
    def setUp(self):
        self.m = load_atc({})
        self.old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        self.addCleanup(self.restore_tz)

    def restore_tz(self):
        if self.old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.old_tz
        time.tzset()

    def check(self, cron, after, expected):
        result = self.m.cron_next(cron, self.m.parse_ts(after))
        self.assertEqual(result, self.m.parse_ts(expected), (cron, after, expected))

    def test_numbers_lists_ranges_steps_and_strict_next_minute(self):
        cases = [
            ("* * * * *", "2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z"),
            ("*/15 * * * *", "2026-01-01T00:14:59Z", "2026-01-01T00:15:00Z"),
            ("5,20,40 8-10 * * *", "2026-01-01T08:20:00Z", "2026-01-01T08:40:00Z"),
            ("10-40/10 9 * * *", "2026-01-01T09:39:59Z", "2026-01-01T09:40:00Z"),
            ("5/20 9 * * *", "2026-01-01T09:25:00Z", "2026-01-01T09:45:00Z"),
            ("7 9 26 9 *", "2026-09-26T09:07:00Z", "2027-09-26T09:07:00Z"),
            ("0 0 29 2 *", "2025-03-01T00:00:00Z", "2028-02-29T00:00:00Z"),
            ("0 0 29 2 *", "2096-03-01T00:00:00Z", "2104-02-29T00:00:00Z"),
        ]
        for cron, after, expected in cases:
            with self.subTest(cron=cron, after=after):
                self.check(cron, after, expected)

    def test_dom_dow_or_and_wildcards(self):
        for cron in ("0 9 13 * 1", "0 9 13 * 1,3"):
            self.check(cron, "2026-01-11T12:00:00Z", "2026-01-12T09:00:00Z")
            self.check(cron, "2026-01-12T12:00:00Z", "2026-01-13T09:00:00Z")
        self.check("0 9 * * 1", "2026-01-12T12:00:00Z", "2026-01-19T09:00:00Z")
        self.check("0 9 13 * *", "2026-01-11T12:00:00Z", "2026-01-13T09:00:00Z")
        self.check("0 9 */2 * 1", "2026-01-11T12:00:00Z", "2026-01-19T09:00:00Z")
        for sunday in ("0", "7", "6-7"):
            self.check("0 9 * * " + sunday, "2026-01-10T12:00:00Z", "2026-01-11T09:00:00Z")

    def test_invalid_and_impossible(self):
        for cron in (None, "", "* * * *", "* * * * * *", "60 * * * *", "* 24 * * *", "* * 0 * *",
                     "* * * 13 *", "* * * * 8", "*/0 * * * *", "1-0 * * * *", "1,,2 * * * *", "a * * * *"):
            with self.subTest(cron=cron), self.assertRaises(ValueError):
                self.m.cron_next(cron, time.time())
        self.assertIsNone(self.m.cron_next("0 0 31 2 *", time.time()))

    def test_local_dst_skipped_and_repeated_minutes(self):
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        self.check("30 2 * * *", "2026-03-08T00:00:00-05:00", "2026-03-09T02:30:00-04:00")
        self.check("30 1 * * *", "2026-11-01T01:30:00-04:00", "2026-11-01T01:30:00-05:00")
        self.check("* * * * *", "2026-11-01T01:45:00-04:00", "2026-11-01T01:46:00-04:00")
        self.check("0,30 1 * * *", "2026-11-01T01:45:00-04:00", "2026-11-01T01:00:00-05:00")
        os.environ["TZ"] = "Australia/Lord_Howe"
        time.tzset()
        self.check("30 2 * * *", "2026-10-04T01:00:00+10:30", "2026-10-04T02:30:00+11:00")


class LaterCase(unittest.TestCase, HTTP):
    def setUp(self):
        self.fx = fixture.LaterFixture()
        self.addCleanup(self.fx.stop)
        self.env = self.fx.env(ATC_PEER_TOKEN="")
        patch = mock.patch.dict(os.environ, self.env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.m = load_atc(self.env)
        self.poller = self.m.Later(background=False)
        feed = self.m.Feed([self.env["ATC_CLAUDE"]], background=False)
        feed.fetch()
        self.fleet = self.m.Fleet(feed=feed, later=self.poller)
        self.fleet.refresh(time.time())

    def session(self, name="alpha-11"):
        return self.fleet.sessions[self.fx.sid(name)]

    def call(self, session, name, tid="call", at=None, **inp):
        record = fixture.assistant(0, fixture.tool(tid, name, **inp))
        if at is not None:
            record["timestamp"] = self.m.iso(at)
        session.ingest(record, self.fleet)

    def service_for(self, fleet=None, remotes=()):
        service = self.m.Service(fleet or self.fleet, None, list(remotes), self.m.Terminals(), token="fixture-token",
                                 background=False, web_origin=ORIGIN, web_dir=os.path.join(self.fx.root, "web"),
                                 peer_secret="")
        service.refresh()
        return service

    def script(self, name, body):
        path = Path(self.poller.directory, name)
        path.parent.mkdir(exist_ok=True)
        path.write_text("#!" + sys.executable + "\n" + body)
        path.chmod(0o755)
        return path

    def test_fixture_schedules_model_needs_and_screen(self):
        now = time.time()
        model = self.fleet.model(now)

        self.assertEqual({n["title"] for n in model["later"]}, {"Check the build", "New wake", "Scheduled cron"})
        self.assertEqual([n["when"] for n in model["later"]], sorted(n["when"] for n in model["later"]))
        wake = next(n for n in model["later"] if n["title"] == "Check the build")
        self.assertAlmostEqual(wake["in"], 720, delta=3)
        self.assertEqual(wake["sid"], self.fx.sid("alpha-11"))
        due = [n for n in model["needs"] if n["kind"] == "later"]
        self.assertIn("Check the build", [n["title"] for n in due])
        self.assertNotIn("New wake", [n["title"] for n in due])
        self.assertTrue(all(n["in"] <= 3600 for n in due))
        view = next(v for g in model["groups"].values() for v in g if v["sid"] == wake["sid"])
        self.assertEqual(view["wakes_at"], wake["when"])
        row = self.m.plain(self.m.session_lines(view, 18, now)[0][0])
        self.assertRegex(row, r"wakes in 1[12]m")
        expanded = "\n".join(self.m.plain(line) for line, _ in self.m.fleet_lines(model, 200, True))
        folded = "\n".join(self.m.plain(line) for line, _ in self.m.fleet_lines(model, 200, False))
        self.assertLess(expanded.index("Waiting on you"), expanded.index("── Later"))
        self.assertIn("New wake", expanded)
        self.assertNotIn("New wake", folded)
        self.assertIn("p lists them", folded)

    def test_overdue_boundary_and_undated_items(self):
        now = time.time()
        self.poller.items = [dict(title=title, when=stamp, kind="study", source="cards", sid=None, host=None)
                             for title, stamp in [("undated", None), ("far", now + 3601), ("due", now + 3600),
                                                  ("overdue", now - 5)]]

        model = self.fleet.model(now)

        plugin_needs = [n for n in model["needs"] if n.get("source") == "cards"]
        self.assertEqual([n["title"] for n in plugin_needs], ["overdue", "due"])
        self.assertEqual(plugin_needs[0]["later_kind"], "study")
        self.assertEqual(model["later"][-1]["title"], "undated")
        self.assertIsNone(model["later"][-1]["in"])
        self.assertEqual(model["counts"]["needs"], len(model["needs"]))

    def test_wake_fires_stop_supersession_errors_and_helpers(self):
        session = self.session()
        self.call(session, "ScheduleWakeup", delaySeconds=0, reason="Ready")
        session.ingest(fixture.assistant(-1, fixture.text("awake")), self.fleet)
        self.assertIsNone(session.wakeup)
        self.call(session, "ScheduleWakeup", delaySeconds=100, reason="Again", noop=True)
        self.assertIsNotNone(session.wakeup)
        self.call(session, "ScheduleWakeup", stop=True)
        self.assertIsNone(session.wakeup)
        self.call(session, "ScheduleWakeup", delaySeconds=100)
        failed = fixture.result(0, "call")
        failed["message"]["content"][0]["is_error"] = True
        session.ingest(failed, self.fleet)
        self.assertIsNone(session.wakeup)
        for inp in ({"delaySeconds": "120"}, {"delaySeconds": -1}, {"delaySeconds": float("inf")},
                    {"delaySeconds": 1e100}, {}):
            self.call(session, "ScheduleWakeup", **inp)
            self.assertIsNone(session.wakeup)
        helper = self.m.Agent()
        self.call(helper, "ScheduleWakeup", delaySeconds=100)
        self.call(helper, "CronCreate", cron="* * * * *")
        self.assertIsNone(helper.wakeup)
        self.assertFalse(helper.crons)

    def test_cron_creation_deletion_results_and_lifetime(self):
        session = self.session()
        self.assertIn("daily", session.crons)
        self.call(session, "CronDelete", id="daily")
        self.assertFalse(session.crons)
        created = (int(time.time()) // 60 + 1) * 60 + 10
        self.call(session, "CronCreate", tid="cron-new", at=created, cron="* * * * *", recurring=False, prompt="Once")
        result = fixture.result(0, "cron-new", "Created job job-one (every minute)")
        result["timestamp"] = self.m.iso(created + 1)
        session.ingest(result, self.fleet)
        self.assertIn("job-one", session.crons)
        due = session.crons["job-one"]["when"]
        self.assertEqual(next(n for n in session.later(due + 1) if n["kind"] == "cron")["when"], due)
        session.ingest({"type": "assistant", "timestamp": self.m.iso(due + 1), "message": {}}, self.fleet)
        self.assertFalse([n for n in session.later(due + 1) if n["kind"] == "cron"])
        self.call(session, "CronDelete", ids=["job-one"])
        self.call(session, "CronCreate", tid="repeat", cron="* * * * *", recurring=True)
        due = session.crons["repeat"]["when"]
        next_run = next(n for n in session.later(due + 1) if n["kind"] == "cron")
        self.assertEqual(next_run["when"], due + 60)
        self.call(session, "CronCreate", tid="bad", cron="invalid")
        self.assertNotIn("bad", session.crons)
        self.call(session, "CronCreate", tid="failed", cron="* * * * *")
        result = fixture.result(0, "failed")
        result["message"]["content"][0]["is_error"] = True
        session.ingest(result, self.fleet)
        self.assertNotIn("failed", session.crons)
        self.fleet.feed.entries = []
        self.fleet.refresh(time.time())
        self.assertEqual(self.fleet.model(time.time())["later"], [])

    def test_transcript_rewrite_clears_schedule_state(self):
        session = self.session()
        fixture.write_jsonl(session.tail.path, [fixture.assistant(0, fixture.text("Reset"))])

        session.poll(self.fleet)

        self.assertIsNone(session.wakeup)
        self.assertFalse(session.crons)

    def test_plugin_good_bad_slow_and_cache_recovery(self):
        due = self.m.iso(time.time() + 300)
        good = self.script("good ; literal", "import json\nprint(json.dumps([dict(title='<script>cards</script>', when=" +
                           repr(due) + ", detail='Review', kind='study', source='cards')]))\n")
        bad = self.script("bad", "print('not JSON')\n")
        self.script("slow", "import time\ntime.sleep(30)\n")
        ignored = self.script("ignored", "raise RuntimeError()\n")
        ignored.chmod(0o644)
        with mock.patch.object(self.m, "LATER_TIMEOUT", 1):
            self.poller.fetch()
        self.assertEqual([n["title"] for n in self.poller.items], ["<script>cards</script>"])
        self.assertEqual(len(self.poller.errors), 2)
        self.assertTrue(any("slow" in e for e in self.poller.errors))
        with mock.patch.object(self.m.subprocess, "run", side_effect=AssertionError("cached/model must not run plug-ins")):
            self.poller.fetch()
            self.fleet.model(time.time())
        self.assertEqual(len(self.poller.errors), 2)
        bad.write_text(good.read_text())
        Path(self.poller.directory, "slow").unlink()
        self.poller.checked -= 601
        self.poller.fetch()
        self.assertEqual(self.poller.errors, [])
        self.assertEqual(len(self.poller.items), 2)

    def test_plugin_bad_shapes_are_atomic_and_missing_folder_is_empty(self):
        self.poller.fetch()
        self.assertEqual((self.poller.items, self.poller.errors), ([], []))
        good = {"title": "OK", "when": None}
        invalid = [{}, [None], [good, {"title": "bad", "when": "tomorrow"}], [{"title": "bad", "when": 17}],
                   [{"title": "bad", "when": "1234"}], [{"title": "bad", "detail": []}]]
        for payload in invalid:
            self.script("shape", "print(" + repr(json.dumps(payload)) + ")\n")
            self.poller.checked = None
            self.poller.fetch()
            self.assertEqual(self.poller.items, [])
            self.assertEqual(len(self.poller.errors), 1)

    def test_empty_override_disables_plugins_and_default_timeout_is_30_seconds(self):
        with mock.patch.dict(os.environ, {"ATC_LATER_DIR": ""}):
            disabled = self.m.Later(background=False)
        with mock.patch.object(self.m.subprocess, "run", side_effect=AssertionError("disabled")):
            disabled.fetch()
        self.assertEqual(disabled.items, [])
        self.script("check", "print('[]')\n")
        with mock.patch.object(self.m.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b'[]')) as run:
            self.poller.fetch()
        self.assertEqual(run.call_args.kwargs["timeout"], 30)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_background_plugin_does_not_block_screen_or_overlap(self):
        entered, release = threading.Event(), threading.Event()
        self.script("wait", "print('[]')\n")

        def run(*args, **kwargs):
            entered.set()
            release.wait(3)
            return subprocess.CompletedProcess(args, 0, b'[{"title":"Done","when":null}]')

        with mock.patch.object(self.m.subprocess, "run", side_effect=run) as process:
            worker = threading.Thread(target=self.poller.fetch)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                model = self.fleet.model(time.time())
                self.m.fleet_lines(model, 120, True)
                self.poller.fetch()
                self.assertTrue(worker.is_alive())
                process.assert_called_once()
            finally:
                release.set()
                worker.join(3)
        self.assertEqual(self.poller.items[0]["title"], "Done")

    def test_http_web_json_mcp_and_host_merge(self):
        self.service = self.service_for()
        self.service.model["machine"] = "laptop"
        self.url = self.start(self.service)
        _, cookie = self.enroll(Passkey(self.fx.root))
        status, web, _ = self.request("/api/web/model", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(web["later"], self.service.model["later"])
        self.assertEqual(self.request("/api/web/model")[0], 401)
        remote = self.m.RemoteAtc(self.url, "fixture-token", background=False)
        remote.fetch()
        remote.host = "laptop"
        self.assertIsNone(remote.error)
        feed = self.m.Feed(None, background=False)
        feed.entries = []
        empty = self.m.Fleet(feed=feed)
        hub = self.service_for(empty, [remote])
        self.assertEqual(len(hub.model["later"]), len(web["later"]))
        self.assertTrue(all(n["host"] == "laptop" for n in hub.model["later"]))
        self.assertTrue(any(n["sid"] and n["host"] == "laptop" for n in hub.model["needs"] if n["kind"] == "later"))
        self.assertEqual(hub.get("/api/fleet")[1]["later"], [])
        # No nested host echo, and cached remote timing ages against the current clock.
        remote.data["later"].append(dict(web["later"][0], host="server1", title="must not loop"))
        remote.error = "offline"
        future = empty.model(time.time() + 7200, remotes=[remote])
        self.assertNotIn("must not loop", [n["title"] for n in future["later"]])
        self.assertTrue(all(n["stale"] for n in future["later"]))
        self.assertTrue(any(n["title"] == "New wake" for n in future["needs"]))
        summary = self.m.tool_needs_me(self.service.cockpit, {}, self.m.Call())
        self.assertIn("Later today:", summary)
        self.assertIn("New wake", summary)
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "atc_later", "arguments": {}}}
        status, reply, _ = self.request("/mcp", request, headers={"Authorization": "Bearer fixture-token", "Origin": None})
        self.assertEqual(status, 200)
        self.assertFalse(reply["result"]["isError"])
        self.assertIn("Check the build", reply["result"]["content"][0]["text"])

    def test_cli_json_once_and_mcp_stdio_include_plugins(self):
        self.script("cards", "print('[{\"title\":\"Study notes\",\"when\":null,\"source\":\"cards\"}]')\n")
        for args in (("--json",), ("--once", "--width", "200"), ("mcp",)):
            request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "atc_later", "arguments": {}}}
            proc = subprocess.run([sys.executable, fixture.ATC, *args, "--no-hosts"], env=self.env,
                                  input=json.dumps(request) + "\n", capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            if args[0] == "--json":
                data = json.loads(proc.stdout)
                self.assertEqual(data["later"][-1]["title"], "Study notes")
                self.assertIn("later", data)
            elif args[0] == "--once":
                self.assertIn("── Later", proc.stdout)
                self.assertNotIn("Study notes", proc.stdout)  # undated: full list only
            else:
                self.assertIn("Study notes", proc.stdout)
            self.assertIn("Check the build", proc.stdout)


if __name__ == "__main__":
    unittest.main()
