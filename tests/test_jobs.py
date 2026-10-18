"""Loom's CLI boundary, model, screen controls, MCP and passkey web paths, using only the fixture stub."""
import http.client
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import threading
import time
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import fixture
from test_units import load_atc
from test_web import HTTP, ORIGIN, OPENSSL, Passkey


class Jobs(unittest.TestCase, HTTP):
    def setUp(self):
        self.fx = fixture.AmailFixture()
        self.addCleanup(self.fx.stop)
        self.env = self.fx.env(ATC_LOOM=os.path.join(self.fx.stub, "loom"), ATC_LOOM_HOSTS="server1",
                               ATC_MCP_LEVERS="confirm", ATC_PEER_TOKEN="", ATC_NOTIFY_CMD="")
        patch = mock.patch.dict(os.environ, self.env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.m = load_atc(self.env)
        self.loom = self.m.Loom(background=False)
        self.loom.fetch()
        feed = self.m.Feed([self.env["ATC_CLAUDE"]], background=False)
        feed.fetch()
        self.fleet = self.m.Fleet(feed=feed, loom=self.loom)
        self.fleet.refresh(time.time())
        self.service = self.m.Service(self.fleet, None, [], self.m.Terminals(), writes=True, token="fixture-token",
                                      background=False, web_origin=ORIGIN,
                                      web_dir=os.path.join(self.fx.root, "web"), peer_secret="")
        self.service.refresh()
        self.ck = self.service.cockpit
        self.fx.clear_calls()

    def refresh(self):
        self.loom.checked = None
        self.loom.fetch()
        self.service.refresh()
        return self.service.model

    def cli(self, *args, **env):
        result = subprocess.run([sys.executable, fixture.ATC, *args], env=dict(self.env, **env),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        return result.stdout

    def test_model_sort_needs_and_apps(self):
        model = self.service.model
        self.assertEqual(set(model["jobs"]), {"local", "server1"})
        local = model["jobs"]["local"]
        self.assertEqual(set(local), {"ok", "error", "jobs", "apps", "apps_ok", "apps_error"})
        self.assertTrue(local["ok"])
        self.assertIsNone(local["error"])
        self.assertEqual([j["name"] for j in local["jobs"]], ["crashed", "running", "due-next", "later", "expired"])
        self.assertIsNone(local["jobs"][-1]["next_run"])
        self.assertEqual(local["apps"][0]["name"], "dashboard")
        self.assertTrue(local["apps_ok"])
        self.assertIsNone(local["apps_error"])
        self.assertEqual(model["jobs"]["server1"]["apps"], [])
        self.assertIsNone(model["jobs"]["server1"]["apps_ok"])
        self.assertEqual(model["counts"]["jobs_failing"], 2)
        needs = [n for n in model["needs"] if n["kind"] == "job"]
        self.assertEqual([(n["host"], n["job"]) for n in needs], [("local", "crashed"), ("server1", "remote-failure")])
        self.assertEqual((model["counts"]["needs"], model["counts"]["needs_urgent"]), (4, 1))
        self.assertEqual(self.m.needs_counts(needs), {"needs": 2, "needs_urgent": 0})
        kinds = [n["kind"] for n in model["needs"]]
        self.assertLess(kinds.index("job"), kinds.index("waiting"))
        self.assertTrue(all(n["sid"] is None and "Open Jobs" in n["hint"] for n in needs))

    def test_missing_and_disabled_loom_are_silent(self):
        for cmd in ("", os.path.join(self.fx.stub, "missing")):
            with self.subTest(cmd=cmd), mock.patch.dict(os.environ, ATC_LOOM=cmd):
                loom = self.m.Loom(background=False)
                loom.fetch()
                self.assertIsNone(loom.cmd)
                self.assertEqual(loom.data, {})
                fleet = self.m.Fleet(loom=loom)
                model = fleet.model(time.time())
                self.assertEqual(model["counts"]["jobs_failing"], 0)
                self.assertEqual(self.m.jobs_lines(model, 120), [])
        self.assertEqual(self.fx.calls(), "")
        disabled = json.loads(self.cli("--json", ATC_LOOM=""))
        self.assertEqual(disabled["jobs"], {})
        self.assertNotIn("Jobs & apps", self.cli("--once", ATC_LOOM=""))

    def test_path_default_and_command_override(self):
        with mock.patch.dict(os.environ):
            del os.environ["ATC_LOOM"]
            loom = self.m.Loom(background=False)
            loom.fetch()
            self.assertEqual(loom.cmd, [self.env["ATC_LOOM"]])
        with mock.patch.dict(os.environ, ATC_LOOM=shlex.join([sys.executable, self.env["ATC_LOOM"]])):
            loom = self.m.Loom(background=False)
            loom.fetch()
            self.assertEqual(loom.data, self.loom.data)
        with mock.patch.dict(os.environ), mock.patch.object(self.m.shutil, "which", return_value=None):
            del os.environ["ATC_LOOM"]
            loom = self.m.Loom(background=False)
            loom.fetch()
            self.assertEqual(loom.data, {})

    def test_host_failure_retains_last_seen_and_recovers(self):
        path = Path(self.fx.stub, "loom-fail-server1")
        path.touch()
        model = self.refresh()
        self.assertTrue(model["jobs"]["local"]["ok"])
        self.assertFalse(model["jobs"]["server1"]["ok"])
        self.assertIn("host unavailable", model["jobs"]["server1"]["error"])
        item = next(n for n in model["needs"] if n["host"] == "server1")
        self.assertIn("Last seen", item["hint"])
        self.assertEqual(model["counts"]["jobs_failing"], 2)
        with self.assertRaisesRegex(self.m.ToolError, "isn't answering"):
            self.m.run_job(self.loom, "server1", "remote-failure")
        path.unlink()
        self.assertTrue(self.refresh()["jobs"]["server1"]["ok"])

    def test_bad_json_does_not_destroy_last_good_jobs(self):
        for bad in ("broken", "{}", '[{"name": 1}]',
                    '[{"name": "odd", "last_run": 1e300, "schedule_raw": {"every": 60}}]'):
            Path(self.fx.stub, "loom-jobs-local.json").write_text(bad)
            self.refresh()
            self.assertFalse(self.loom.data["local"]["ok"])
            self.assertEqual(len(self.loom.data["local"]["jobs"]), 5)
            self.assertTrue(self.loom.data["server1"]["ok"])
        Path(self.fx.stub, "loom-apps.json").write_text("null")
        self.refresh()
        self.assertIn("apps:", self.loom.data["local"]["apps_error"])

    def test_app_failure_does_not_disable_job_status_or_run(self):
        Path(self.fx.stub, "loom-fail-apps").touch()
        model = self.refresh()
        local = model["jobs"]["local"]
        self.assertTrue(local["ok"])
        self.assertIsNone(local["error"])
        self.assertFalse(local["apps_ok"])
        self.assertIn("app registry unavailable", local["apps_error"])
        self.assertEqual(local["apps"][0]["name"], "dashboard")
        self.assertTrue(all("Last seen" not in n["hint"] for n in model["needs"] if n["kind"] == "job"))
        self.assertIn("Apps unavailable", self.m.tool_jobs(self.ck, {}, self.m.Call()))
        self.assertIn("Recent log", self.m.tool_jobs(self.ck, {"job": "crashed"}, self.m.Call()))
        result = self.service.run_tool("web", "atc_run_job", {"arguments": {"job": "crashed"}}, "Phone")
        self.assertTrue(result["ok"])
        self.assertIn("loom jobs run crashed", self.fx.calls())
        Path(self.fx.stub, "loom-fail-apps").unlink()
        self.assertTrue(self.refresh()["jobs"]["local"]["apps_ok"])

    def test_stalled_poll_workers_cannot_hold_process_exit(self):
        script = '''import runpy, sys, threading
m = runpy.run_path(sys.argv[1])
started = threading.Event()
def stalled(self, host, kind):
    assert threading.current_thread().daemon
    started.set()
    threading.Event().wait(30)
m["Loom"].read = stalled
loom = m["Loom"]()
assert started.wait(2), "poll did not start"
assert all(t.daemon for t in threading.enumerate() if t is not threading.main_thread())
print("stalled daemon poll started", flush=True)
'''
        result = subprocess.run([sys.executable, "-c", script, fixture.ATC], env=self.env,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stalled daemon poll started", result.stdout)

    def test_sixty_second_cache_including_failures(self):
        checked = self.loom.checked = 100.0
        Path(self.fx.stub, "loom-fail-server1").touch()
        with mock.patch.object(self.m.time, "monotonic", return_value=checked + 59):
            self.loom.fetch()
        self.assertEqual(self.fx.calls(), "")
        with mock.patch.object(self.m.time, "monotonic", return_value=checked + 60):
            self.loom.fetch()
            self.loom.fetch()
        self.assertEqual(sorted(self.fx.calls().splitlines()), ["loom --host server1 jobs list --json",
                         "loom jobs list --json", "loom list --json"])
        self.assertFalse(self.loom.data["server1"]["ok"])

    def test_loom_hosts_are_independent_of_agent_peer_availability(self):
        peer = self.m.Remote("server1", background=False)
        peer.error = "agent peer is asleep"
        peer.data = {"needs": [self.m.need_item("job", "remote-failure")]}
        model = self.fleet.model(time.time(), remotes=[peer])
        items = [n for n in model["needs"] if n["kind"] == "job"]
        self.assertEqual(len(items), 2)
        self.assertTrue(all("Last seen" not in n["hint"] for n in items))

    def test_reads_run_in_parallel_with_timeouts(self):
        barrier = threading.Barrier(3)
        calls = []

        def read(args, timeout):
            calls.append((args, timeout))
            barrier.wait(timeout=3)
            return 0, "[]", ""

        self.loom.checked = None
        with mock.patch.object(self.m, "run", side_effect=read):
            self.loom.fetch()
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(timeout == 15 for _, timeout in calls))
        Path(self.fx.stub, "loom-delay").write_text("1")
        self.loom.TIMEOUT = 0.05
        with mock.patch.object(self.m, "run", wraps=self.m.run) as timed:
            self.refresh()
        self.assertTrue(all(not e["ok"] for e in self.loom.data.values()))
        self.assertEqual(timed.call_count, 3)

    def test_schedule_estimates(self):
        now = datetime(2026, 10, 17, 10, 15, 30).timestamp()
        next_run = lambda schedule, **kwargs: self.m.job_next_run(dict(schedule_raw=schedule, **kwargs), now)
        self.assertEqual(next_run({"every": 300}, last_run=now - 60), now + 240)
        self.assertIsNone(next_run({"every": 300}))
        self.assertEqual(next_run({"at": now + 100}), now + 100)
        self.assertIsNone(next_run({"at": now - 100}, last_run=now))
        self.assertIsNone(next_run({"every": 300}, last_run=now, expired=True))
        self.assertIsNone(next_run({"every": 300}, last_run=now, running=True))
        self.assertEqual(next_run({"calendar": {"Hour": 11, "Minute": 5}}), datetime(2026, 10, 17, 11, 5).timestamp())
        self.assertEqual(next_run({"calendar": {"Weekday": 0, "Hour": 9, "Minute": 0}}), datetime(2026, 10, 18, 9).timestamp())
        for schedule in ({"every": -1}, {"every": float("inf")}, {"calendar": {"Hour": 25}}, "odd", {"calendar": {"Day": 31, "Month": 2}}):
            self.assertIsNone(next_run(schedule))

    def test_human_due_times_in_screen_mcp_and_machine_timestamps_in_json(self):
        now = datetime(2026, 10, 17, 7).timestamp()
        times = [datetime(2026, 10, 17, 7, 12).timestamp(), datetime(2026, 10, 17, 8, 5).timestamp(),
                 datetime(2026, 10, 18, 1, 30).timestamp()]
        rows = [{"name": name, "schedule_raw": {"at": at}} for name, at in zip(("soon", "today", "tomorrow"), times)]
        Path(self.fx.stub, "loom-jobs-local.json").write_text(json.dumps(rows))
        with mock.patch.object(self.m.time, "time", return_value=now):
            self.refresh()
        model = self.service.model
        screen = "\n".join(self.m.plain(line) for line, _ in self.m.jobs_lines(model, 200, expanded=True))
        mcp = self.m.tool_jobs(self.ck, {}, self.m.Call())
        for expected in ("in 12m", "at 08:05", "tomorrow 01:30"):
            self.assertIn(expected, screen)
            self.assertIn(expected, mcp)
        for text in (screen, mcp):
            self.assertNotIn("2026-10-17T", text)
            self.assertNotIn("2026-10-18T", text)
        jobs = json.loads(json.dumps(self.service.web_model()))["jobs"]["local"]["jobs"]
        self.assertEqual([j["next_run"] for j in jobs], times)
        self.assertEqual([j["next_run_iso"] for j in jobs], [self.m.iso(at) for at in times])
        self.assertEqual(self.m.job_due(now - 720, now), "overdue by 12m")

    def test_render_is_cached_idle_folded_and_rows_resolve(self):
        with mock.patch.object(self.loom, "fetch", side_effect=AssertionError("screen must not poll")), \
                mock.patch.object(self.m, "run", side_effect=AssertionError("screen must not run commands")):
            self.service.refresh()
            model = self.service.model
            rows = self.m.fleet_lines(model, 180, True)
            keys = [key for _, key in rows if key]
            text = "\n".join(self.m.plain(line) for line, _ in rows)
            self.assertIn("2 idle jobs (1 expired)", text)
            self.assertLess(text.index("crashed  failing"), text.index("running  running"))
            self.assertLess(text.index("running  running"), text.index("due-next  "))
            failing = next(line for line, key in rows if key == self.m.job_key("local", "crashed"))
            self.assertTrue(all(style == "warn" for _, style in failing))
            notice = next(line for line, key in rows if line[0][0] == "crashed on local")
            self.assertEqual(notice[0][1], "warn")
            self.assertIn(("2 failing jobs · ", "warn"), self.m.header_line(model))
            self.assertEqual(len(keys), len(set(keys)))
            key = self.m.job_key("server1", "remote-failure")
            item = self.m.resolve(self.fleet, model, key)
            self.assertEqual(item[:2], ("job", "server1"))
            self.m.selection_lines(self.fleet, model, item, 0, 100)
            expanded = self.m.fleet_lines(model, 180, True, True)
            self.assertIn(self.m.job_key("local", "expired"), [key for _, key in expanded])
        with mock.patch.object(self.m.threading, "Thread") as thread:
            loom = self.m.Loom()
            self.assertEqual(thread.call_args.kwargs["target"], loom.loop)
            thread.return_value.start.assert_called_once()

    def test_screen_status_and_two_press_run_are_background_and_audited(self):
        app = self.m.App(self.fleet, None, self.m.Terminals(), None, ["claude"], False)
        key = self.m.job_key("server1", "remote-failure")
        app.ui["selected"] = key
        app.selectable = [key]
        app.handle("\n", self.service.model)
        end = time.monotonic() + 3
        while self.loom.details.get(("server1", "remote-failure")) == "Reading loom status…" and time.monotonic() < end:
            time.sleep(0.01)
        self.assertIn("Recent log: fixture job output", self.loom.details[("server1", "remote-failure")])
        self.assertEqual(app.ui["view"], "detail")
        app.handle("\x1b", self.service.model)
        app.handle("x", self.service.model)
        self.assertIn("press x again", app.flash[0])
        self.assertNotIn("jobs run", self.fx.calls())
        app.handle("x", self.service.model)
        end = time.monotonic() + 3
        while not app.flash[0].startswith("triggered") and time.monotonic() < end:
            time.sleep(0.01)
        self.assertIn("triggered remote-failure on server1", app.flash[0])
        self.assertIn("loom --host server1 jobs run remote-failure", self.fx.calls())
        self.assertIn('"transport": "screen"', Path(self.m.AUDIT_LOG).read_text())

    @unittest.skipUnless(fixture.TMUX, "tmux is needed for the job screen test")
    def test_real_screen_status_two_press_run_and_idle_toggle(self):
        name = f"atc-jobs-ui-{os.getpid()}"
        exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in self.env.items()
                           if k.startswith(("ATC_", "CLAUDE_", "ROUTE_")) or k in ("HOME", "PATH", "TERM"))
        command = f"env {exports} {shlex.quote(sys.executable)} {shlex.quote(fixture.ATC)} --no-hosts"
        try:
            result = subprocess.run([fixture.TMUX, "new-session", "-d", "-s", name, "-x", "200", "-y", "80", command],
                                    capture_output=True, text=True)
        except PermissionError as exc:
            self.skipTest(f"sandbox denied tmux for the job screen test: {exc}")
        if result.returncode and any(s in result.stderr.lower() for s in ("operation not permitted", "permission denied")):
            self.skipTest(f"sandbox denied the tmux socket: {result.stderr.strip()}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(subprocess.run, [fixture.TMUX, "kill-session", "-t", name], capture_output=True)

        def screen():
            return subprocess.run([fixture.TMUX, "capture-pane", "-p", "-t", name], capture_output=True, text=True).stdout

        def wait_for(needle):
            until = time.monotonic() + 8
            while time.monotonic() < until:
                text = screen()
                if needle in text:
                    return text
                time.sleep(0.05)
            self.fail(f"screen did not show {needle!r}:\n{screen()}")

        def press(key):
            subprocess.run([fixture.TMUX, "send-keys", "-t", name, key], check=True, capture_output=True)
            time.sleep(0.1)

        def select(needle):
            for _ in range(30):
                if any(line.startswith("▶") and needle in line for line in screen().splitlines()):
                    return
                press("Down")
            self.fail("could not select " + needle + ":\n" + screen())

        wait_for("4 Jobs")
        press("4")
        wait_for("2 idle jobs (1 expired)")
        select("crashed")
        press("Enter")
        wait_for("Recent log: fixture job output")
        press("Escape")
        press("x")
        wait_for("press x again")
        self.assertNotIn("jobs run", self.fx.calls())
        press("x")
        wait_for("triggered crashed on local")
        self.assertEqual(self.fx.calls().count("loom jobs run crashed"), 1)
        press("p")
        select("expired")
        press("Enter")
        wait_for("expired on local")
        press("Escape")
        press("Down")
        press("Enter")
        wait_for("remote-failure on server1")
        self.assertIn("loom --host server1 jobs status remote-failure", self.fx.calls())
        press("Escape")
        press("q")

    def test_mcp_read_dry_run_confirmation_real_run_and_refusals(self):
        server = self.m.McpServer(self.ck, "http")
        ctx = {"modern": True, "caps": {"elicitation": {"form": {}}}, "client": None}
        read = server.call_tool({"name": "atc_jobs", "arguments": {}}, ctx, None)
        self.assertIn("Jobs & apps on server1", read["content"][0]["text"])
        self.assertIn("app dashboard", read["content"][0]["text"])
        args = {"job": "remote-failure", "host": "server1"}
        params = {"name": "atc_run_job", "arguments": dict(args, dry_run=True)}
        preview = server.call_tool(params, ctx, None)
        self.assertIn("--host server1 jobs run remote-failure", preview["content"][0]["text"])
        self.assertEqual(self.fx.calls(), "")
        params["arguments"] = args
        asked = server.call_tool(params, ctx, None)
        self.assertEqual(asked["resultType"], "input_required")
        self.assertEqual(self.fx.calls(), "")
        params.update(requestState=asked["requestState"], inputResponses={"confirm": {"action": "accept", "content": {"confirm": True}}})
        result = server.call_tool(params, ctx, None)
        self.assertFalse(result["isError"])
        self.assertEqual(self.fx.calls(), "loom --host server1 jobs run remote-failure\n")
        for name, host in (("missing", "local"), ("--help", "local"), ("crashed", "unknown")):
            result = server.call_tool({"name": "atc_run_job", "arguments": {"job": name, "host": host}}, ctx, None)
            self.assertTrue(result["isError"])
        audit = [json.loads(line) for line in Path(self.m.AUDIT_LOG).read_text().splitlines()]
        self.assertEqual([a["outcome"] for a in audit], ["dry run", "asked", "done", "refused", "refused", "refused"])
        off = self.m.McpServer(self.ck, "http", levers=False)
        self.assertTrue(off.call_tool({"name": "atc_run_job", "arguments": args}, ctx, None)["isError"])

    def test_job_approval_shares_needs_queue_and_runs_only_once(self):
        args = {"job": "crashed"}
        ctx = {"modern": True, "caps": {}, "client": None}
        result = self.service.mcp.call_tool({"name": "atc_run_job", "arguments": args}, ctx, "owner-session")
        self.assertIn("Waiting for approval", result["content"][0]["text"])
        self.assertNotIn("jobs run", self.fx.calls())
        self.service.refresh()
        needs = self.service.model["needs"]
        self.assertEqual([n["kind"] for n in needs[:2]], ["blocked", "approval"])
        self.assertEqual(sum(n["kind"] == "job" for n in needs), 2)
        for model in (self.service.web_model(), json.loads(self.cli("--json", "--no-hosts"))):
            self.assertEqual((model["counts"]["needs"], model["counts"]["needs_urgent"]), (5, 2))
        exported = self.service.get("/api/fleet")[1]
        # Its own loom hosts' failing jobs are exported too (server1's as well as local's).
        self.assertEqual((exported["counts"]["needs"], exported["counts"]["needs_urgent"]), (5, 2))
        self.fx.clear_calls()
        row = self.fleet.approvals.access()[0]
        self.fleet.approvals.decide(row["id"], "approve", "owner", "terminal")
        params = {"name": "atc_run_job", "arguments": dict(args, approval=row["id"])}
        result = self.service.mcp.call_tool(params, ctx, "owner-session")
        self.assertFalse(result["isError"])
        self.assertTrue(self.service.mcp.call_tool(params, ctx, "owner-session")["isError"])
        self.assertEqual(self.fx.calls().count("loom jobs run crashed"), 1)
        self.assertEqual(len(self.m.TOOLS), 21)
        self.assertEqual(sum(not tool["lever"] for tool in self.m.TOOLS.values()), 9)

    def test_service_dry_run_reaches_shared_runner_after_approval(self):
        service = self.m.Service(self.fleet, None, [], self.m.Terminals(dry_run=True), writes=True,
                                 token="fixture-token", background=False, peer_secret="")
        service.refresh()
        self.assertTrue(service.cockpit.dry_run)
        args = {"host": "server1", "job": "remote-failure", "dry_run": False}
        lever = self.m.tool_run_job(service.cockpit, args, self.m.Call())
        row = self.fleet.approvals.create("atc_run_job", args, lever.summary, "owner-session", "test-client", lever.target)
        self.fleet.approvals.decide(row["id"], "approve", "owner", "terminal")
        result = service.mcp.call_tool({"name": "atc_run_job", "arguments": dict(args, approval=row["id"])},
                                       {"modern": True, "caps": {}, "client": None}, "owner-session")
        self.assertFalse(result["isError"])
        self.assertIn("dry run:", result["content"][0]["text"])
        self.assertEqual(self.fleet.approvals.get(row["id"])["state"], "used")
        web = service.run_tool("web", "atc_run_job", {"arguments": args}, "Phone")
        self.assertTrue(web["ok"])
        self.assertIn("dry run:", web["text"])
        ok, text = lever.run()  # shared runner also protects callers that build a Lever directly
        self.assertTrue(ok)
        self.assertIn("dry run:", text)
        self.assertNotIn("jobs run", self.fx.calls())
        audit = [json.loads(line) for line in Path(self.m.AUDIT_LOG).read_text().splitlines()]
        self.assertEqual([a["outcome"] for a in audit if a["transport"] in ("web", "http")], ["dry run", "dry run"])

    @unittest.skipUnless(OPENSSL, "openssl is needed for the passkey fixture")
    def test_serve_writes_dry_run_cli_never_runs_confirmed_web_or_mcp_job(self):
        log, errors = Path(self.fx.root, "serve.log"), Path(self.fx.root, "serve-errors.log")
        with log.open("w") as out, errors.open("w") as err:
            proc = subprocess.Popen([sys.executable, fixture.ATC, "--serve", "127.0.0.1:0", "--serve-writes",
                                     "--dry-run", "--no-hosts", "--web-origin", ORIGIN],
                                    env=dict(self.env, ATC_SERVE_TOKEN="fixture-token"), stdout=out, stderr=err)
        def stop():
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=5)
        self.addCleanup(stop)
        end = time.monotonic() + 10
        while time.monotonic() < end:
            address = re.search(r"serving (http://\S+)", log.read_text())
            if address:
                break
            if proc.poll() is not None:
                if "Operation not permitted" in errors.read_text() or "Permission denied" in errors.read_text():
                    self.skipTest("sandbox denied loopback server: " + errors.read_text().splitlines()[-1])
                self.fail(errors.read_text())
            time.sleep(0.05)
        self.assertIsNotNone(address, errors.read_text())
        self.url = address.group(1)
        headers = {"Authorization": "Bearer fixture-token"}
        while time.monotonic() < end:
            status, model, _ = self.request("/api/fleet", headers=headers)
            if status == 200 and model.get("jobs", {}).get("local", {}).get("ok"):
                break
            time.sleep(0.05)
        self.assertEqual(status, 200)
        self.assertTrue(model.get("jobs", {}).get("local", {}).get("ok"), errors.read_text())
        code = self.m.web_enroll(os.path.join(self.fx.root, "home", ".config", "atc", "web"), is_tty=lambda: True)
        options = self.begin(code=code)
        status, _, response_headers = self.finish(Passkey(self.fx.root), options, register=True)
        self.assertEqual(status, 200)
        cookie = response_headers["Set-Cookie"].split(";", 1)[0]
        path = "/api/web/tools/atc_run_job"
        args = {"host": "server1", "job": "remote-failure"}
        self.assertTrue(self.request(path, {"arguments": dict(args, dry_run=True)}, cookie=cookie)[1]["ok"])
        web = self.request(path, {"arguments": dict(args, dry_run=False)}, cookie=cookie)[1]
        self.assertTrue(web["ok"])
        self.assertIn("dry run:", web["text"])
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "atc_run_job", "arguments": args}}
        mcp_headers = {**headers, "Origin": None, "X-Atc-Web": None}  # native MCP client, not a browser request
        status, reply, _ = self.request("/mcp", body, headers=mcp_headers)
        self.assertEqual(status, 200, reply)
        self.assertFalse(reply["result"]["isError"])
        self.assertIn("dry run:", reply["result"]["content"][0]["text"])
        self.assertNotIn("jobs run", self.fx.calls())

    def test_mcp_job_dry_run_http_rejects_browser_origin(self):
        service = self.m.Service(self.fleet, None, [], self.m.Terminals(dry_run=True), writes=True,
                                 token="fixture-token", background=False, peer_secret="")
        service.refresh()
        self.url = "http://127.0.0.1:7070"
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "atc_run_job", "arguments": {"host": "server1", "job": "remote-failure", "dry_run": False}}}
        headers = {"Authorization": "Bearer fixture-token"}

        def dispatch(request, timeout):
            # Exercise the actual HTTP parser and handler without a listening socket.
            payload = request.data or b""
            sent = dict(request.header_items(), **{"Content-Length": str(len(payload))})
            raw = f"{request.get_method()} {request.selector} HTTP/1.0\r\n"
            raw += "".join(f"{key}: {value}\r\n" for key, value in sent.items()) + "\r\n"
            stream, output = mock.Mock(), bytearray()
            stream.makefile.return_value = io.BytesIO(raw.encode() + payload)
            stream.sendall.side_effect = output.extend
            self.m.Handler(stream, ("127.0.0.1", 12345), SimpleNamespace(service=service))
            stream.makefile.return_value = io.BytesIO(output)
            response = http.client.HTTPResponse(stream)
            response.begin()
            return response

        with mock.patch("urllib.request.urlopen", side_effect=dispatch), mock.patch.dict(os.environ, ATC_MCP_ORIGINS=""):
            status, reply, _ = self.request("/mcp", body, headers=headers)
            self.assertEqual(status, 403)
            self.assertIn("Origin " + ORIGIN + " is not allowed", reply["error"]["message"])
            status, reply, _ = self.request("/mcp", body, headers={**headers, "Origin": None, "X-Atc-Web": None})

        self.assertEqual(status, 200, reply)
        self.assertFalse(reply["result"]["isError"])
        self.assertIn("dry run:", reply["result"]["content"][0]["text"])
        self.assertNotIn("jobs run", self.fx.calls())

    def test_rechecks_after_confirmation_and_timeout_is_not_retried(self):
        lever = self.m.tool_run_job(self.ck, {"job": "crashed"}, self.m.Call())
        Path(self.fx.stub, "loom-fail-local").touch()
        self.refresh()
        with self.assertRaisesRegex(self.m.ToolError, "isn't answering"):
            lever.run()
        Path(self.fx.stub, "loom-fail-local").unlink()
        self.refresh()
        with mock.patch.object(self.m, "run", return_value=(None, "", "timeout")) as run:
            ok, text = self.m.run_job(self.loom, "local", "crashed")
        self.assertFalse(ok)
        self.assertIn("may or may not have started", text)
        run.assert_called_once()

    def test_once_json_and_on_demand_stdio(self):
        model = json.loads(self.cli("--json"))
        self.assertEqual(model["counts"]["jobs_failing"], 2)
        once = self.cli("--once", "--width", "200")
        self.assertIn("Jobs & apps", once)
        self.assertIn("2 idle jobs (1 expired)", once)
        self.assertIn("remote-failure", once)
        self.fx.clear_calls()
        requests = [{"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args}}
                    for i, (name, args) in enumerate((("atc_jobs", {}), ("atc_run_job", {"job": "crashed", "dry_run": True}),
                                                     ("atc_run_job", {"job": "crashed"})), 1)]
        result = subprocess.run([sys.executable, fixture.ATC, "mcp", "--no-hosts"],
                                input="".join(json.dumps(r) + "\n" for r in requests),
                                env=dict(self.env, ATC_MCP_LEVERS="auto"), capture_output=True, text=True, timeout=30)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(replies), 3)
        self.assertTrue(all(not r["result"]["isError"] for r in replies))
        self.assertIn("triggered crashed", replies[-1]["result"]["content"][0]["text"])
        self.assertEqual(self.fx.calls().count("loom jobs list --json"), 1)
        self.assertEqual(self.fx.calls().count("loom jobs run crashed"), 1)

    def test_web_model_and_fleet_export(self):
        model = self.service.web_model()
        self.assertEqual(model["jobs"], self.loom.data)
        exported = self.service.get("/api/fleet")[1]
        self.assertEqual(set(exported["jobs"]), {"local", "server1"})  # its own loom hosts, all of them
        self.assertTrue(all(item["kind"] != "job" or not item.get("via") for item in exported["needs"]))
        result = self.service.run_tool("web", "atc_run_job", {"arguments": {"job": "crashed", "dry_run": True}}, "Phone")
        self.assertTrue(result["ok"])
        self.assertEqual(self.fx.calls(), "")

    def test_hub_shows_and_runs_another_atcs_jobs_without_re_exporting_them(self):
        # Arrange: this fixture's atc (with loom) is "home"; a hub without loom reads it over HTTP.
        self.service.peer_secret = "peer-secret"
        url = self.start(self.service)
        remote = self.m.RemoteAtc(url, "fixture-token", background=False)
        remote.fetch()
        remote.host = remote.data["machine"] = "home-box"
        hub_fleet = self.m.Fleet("/nowhere/at/all", loom=None)
        hub = self.m.Service(hub_fleet, None, [remote], self.m.Terminals(), writes=True, token="fixture-token",
                             background=False, peer_secret="peer-secret")
        # Act
        hub.refresh()
        model = hub.model
        # Assert: home's "local" is named home-box, its other loom host keeps its name, both reached through home.
        self.assertEqual(model["jobs"]["home-box"]["via"], "home-box")
        self.assertEqual(model["jobs"]["server1"]["via_key"], "server1")
        failing = [item for item in model["needs"] if item["kind"] == "job"]
        self.assertTrue(failing and {item["host"] for item in failing} <= {"home-box", "server1"})
        home_urgent = self.service.get("/api/fleet")[1]["counts"]["needs_urgent"]
        self.assertEqual(model["counts"]["needs_urgent"], home_urgent)  # failing jobs add nothing urgent
        exported = hub.get("/api/fleet")[1]
        self.assertEqual(exported["jobs"], {})  # the hub never re-exports what it learned from home
        self.assertFalse([item for item in exported["needs"] if item["kind"] == "job"])
        self.fx.clear_calls()
        dry = hub.run_tool("web", "atc_run_job", {"arguments": {"job": "crashed", "host": "home-box",
                                                                "dry_run": True}}, "Phone")
        self.assertTrue(dry["ok"], dry)
        self.assertNotIn("jobs run", self.fx.calls())
        real = hub.run_tool("web", "atc_run_job", {"arguments": {"job": "crashed", "host": "home-box"}}, "Phone")
        self.assertTrue(real["ok"], real)
        self.assertIn("jobs run crashed", self.fx.calls())

    @unittest.skipUnless(OPENSSL, "openssl is needed for the passkey fixture")
    def test_web_json_status_and_run_require_passkey(self):
        self.url = self.start(self.service)
        _, cookie = self.enroll(Passkey(self.fx.root))
        status, model, _ = self.request("/api/web/model", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(model["jobs"], self.loom.data)
        path = "/api/web/job/remote-failure?host=server1"
        self.assertIn("Recent log", self.request(path, cookie=cookie)[1]["text"])
        self.assertEqual(self.request(path)[0], 401)
        path = "/api/web/tools/atc_run_job"
        args = {"arguments": {"host": "server1", "job": "remote-failure", "dry_run": True}}
        self.assertEqual(self.request(path, args, headers={"Authorization": "Bearer fixture-token"})[0], 401)
        self.assertTrue(self.request(path, args, cookie=cookie)[1]["ok"])
        self.assertNotIn("jobs run", self.fx.calls())
        args["arguments"]["dry_run"] = False
        self.assertTrue(self.request(path, args, cookie=cookie)[1]["ok"])
        self.assertIn("loom --host server1 jobs run remote-failure", self.fx.calls())
        self.service.writes = False
        self.service.web_tools.levers = False
        self.assertFalse(self.request(path, args, cookie=cookie)[1]["ok"])


if __name__ == "__main__":
    unittest.main()
