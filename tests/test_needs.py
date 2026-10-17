"""The shared needs queue, using counts-only a-mail and process stubs, without real accounts or agents."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock

import fixture
from test_units import load_atc
from test_web import HTTP, ORIGIN, Passkey


class Needs(unittest.TestCase, HTTP):
    def setUp(self):
        self.node_env = dict(os.environ)  # version-manager shims need the original HOME
        self.fx = fixture.AmailFixture()
        self.addCleanup(self.fx.stop)
        self.env = self.fx.env(ATC_AMAIL=os.path.join(self.fx.stub, "amail"), ATC_PEER_TOKEN="")
        patch = mock.patch.dict(os.environ, self.env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.m = load_atc(self.env)
        self.poller = self.m.AMail(background=False)
        self.poller.fetch()
        feed = self.m.Feed([self.env["ATC_CLAUDE"]], background=False)
        feed.fetch()
        self.fleet = self.m.Fleet(feed=feed, amail=self.poller)
        self.fleet.refresh(time.time())

    def model(self, remotes=()):
        return self.fleet.model(time.time(), remotes=remotes)

    def service_for(self, fleet=None, remotes=()):
        service = self.m.Service(fleet or self.fleet, None, list(remotes), self.m.Terminals(), token="fixture-token",
                                 background=False, web_origin=ORIGIN, web_dir=os.path.join(self.fx.root, "web"),
                                 peer_secret="")
        service.refresh()
        return service

    def write_status(self, status):
        Path(self.fx.stub, "amail-status.json").write_text(json.dumps(status))
        self.poller.checked = None

    def cli(self, *args, **env):
        result = subprocess.run([sys.executable, fixture.ATC, *args], env=dict(self.env, **env),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        return result.stdout

    def test_queue_order_counts_and_contract(self):
        model = self.model()

        self.assertEqual([n["kind"] for n in model["needs"]], ["blocked", "amail_approval", "amail_request",
                         "amail_proposal", "amail_device", "waiting", "amail_inbox"])
        self.assertEqual((model["counts"]["needs"], model["counts"]["needs_urgent"]), (7, 4))
        self.assertEqual((model["counts"]["blocked"], model["counts"]["waiting"]), (1, 1))
        for item in model["needs"]:
            self.assertEqual(set(item), {"kind", "host", "sid", "title", "detail", "since", "hint"})
            self.assertIsNone(item["host"])
            if item["kind"].startswith("amail_"):
                self.assertIsNone(item["sid"])
                self.assertIsNone(item["since"])
                self.assertIn("Roost or the a-mail app", item["hint"])
        self.assertIn("4+ inbox items waiting", model["needs"][-1]["title"])
        self.assertNotIn("owner@amail.example.com", json.dumps(model["needs"]))
        self.assertEqual([c for c in self.fx.calls().splitlines() if c.startswith("amail ")], ["amail status --json"])

    def test_missing_cli_and_disabled_are_silent(self):
        for settings in ({"ATC_AMAIL": os.path.join(self.fx.stub, "missing")}, {"ATC_AMAIL": ""}):
            with self.subTest(settings=settings), mock.patch.dict(os.environ, settings):
                poller = self.m.AMail(background=False)

                poller.fetch()

                self.assertEqual(poller.items, [])
        with mock.patch.dict(os.environ, self.env), mock.patch.object(self.m.shutil, "which", return_value=None):
            os.environ.pop("ATC_AMAIL")
            poller = self.m.AMail(background=False)
            poller.fetch()
            self.assertIsNone(poller.cmd)
            self.assertEqual(poller.items, [])

    def test_default_path_lookup_and_command_override(self):
        with mock.patch.dict(os.environ, {"PATH": self.fx.stub + os.pathsep + os.environ["PATH"]}):
            os.environ.pop("ATC_AMAIL")
            poller = self.m.AMail(background=False)

            poller.fetch()

            self.assertEqual(poller.items, self.poller.items)
        with mock.patch.dict(os.environ, {"ATC_AMAIL": sys.executable + ' "' + self.env["ATC_AMAIL"] + '"'}):
            poller = self.m.AMail(background=False)
            poller.fetch()
            self.assertEqual(poller.items, self.poller.items)

    def test_failure_after_success_one_note_then_recovery(self):
        Path(self.fx.stub, "amail-fail").touch()
        self.poller.checked = None

        self.poller.fetch()
        first = self.model()
        self.poller.checked = None
        self.poller.fetch()

        self.assertEqual(self.poller.items, [self.m.need_item("amail_unreachable", "a-mail unreachable",
                         "Counts are unavailable.", hint="Check Roost or the a-mail app.")])
        self.assertEqual([n["kind"] for n in first["needs"]], [n["kind"] for n in self.model()["needs"]])
        self.assertEqual((first["counts"]["needs"], first["counts"]["needs_urgent"]), (3, 1))
        self.assertIn("a-mail unreachable", self.m.plain(self.m.header_line(first)))

        Path(self.fx.stub, "amail-fail").unlink()
        self.poller.checked = None
        self.poller.fetch()

        self.assertEqual(len(self.poller.items), 5)
        self.assertNotIn("unreachable", json.dumps(self.poller.items))

    def test_initial_failure_and_bad_status_do_not_create_items(self):
        malformed = [None, {}, {"health": {"ok": False}}, fixture.amail_status()]
        malformed[-1]["owner"]["requests"] = -1
        for status in malformed:
            with self.subTest(status=status):
                self.write_status(status)
                poller = self.m.AMail(background=False)

                poller.fetch()

                self.assertEqual(poller.items, [])
                self.assertFalse(poller.worked)

    def test_health_failure_after_success_and_zero_counts(self):
        data = fixture.amail_status()
        data["health"]["ok"] = False
        self.write_status(data)

        self.poller.fetch()

        self.assertEqual([n["kind"] for n in self.poller.items], ["amail_unreachable"])
        data["health"]["ok"] = True
        data["owner"] = dict.fromkeys(data["owner"], 0)
        data["inbox"]["waiting"] = data["device"]["awaitingApproval"] = 0
        self.write_status(data)
        self.poller.fetch()
        self.assertEqual(self.poller.items, [])

    def test_cache_including_failures_expires_after_sixty_seconds(self):
        checked = self.poller.checked
        Path(self.fx.stub, "amail-fail").touch()
        self.fx.clear_calls()

        with mock.patch.object(self.m.time, "monotonic", return_value=checked + 59):
            self.poller.fetch()
        self.assertEqual(self.fx.calls(), "")
        with mock.patch.object(self.m.time, "monotonic", return_value=checked + 60):
            self.poller.fetch()
            self.poller.fetch()

        self.assertEqual(self.fx.calls(), "amail status --json\n")
        self.assertEqual(self.poller.items[0]["kind"], "amail_unreachable")

    def test_model_and_service_do_not_fetch_on_render(self):
        with mock.patch.object(self.poller, "fetch", side_effect=AssertionError("must poll in the background")):
            service = self.service_for()

            service.refresh()
            self.m.render(self.fleet, self.model(), {"selected": None, "show_all": True}, 160)

            self.assertEqual(service.model["counts"]["needs"], 7)
        with mock.patch.object(self.m.threading, "Thread") as thread:
            poller = self.m.AMail()
            self.assertEqual(thread.call_args.kwargs["target"], poller.loop)
            thread.return_value.start.assert_called_once()

    def test_on_demand_mcp_polls_and_caches(self):
        self.fx.clear_calls()
        cockpit = self.m.on_demand_cockpit(use_hosts=False)

        first = self.m.tool_needs_me(cockpit, {}, self.m.Call())
        cockpit.cached_at = 0
        second = self.m.tool_needs_me(cockpit, {}, self.m.Call())

        self.assertEqual(first, second)
        self.assertEqual(self.fx.calls().count("amail status --json"), 1)
        self.assertIn("a-mail 2 approvals", first)
        self.assertIn("asks: permission prompt", first)
        self.assertIn("said: Done. Want me to ship it?", first)
        self.assertLess(first.index("gamma-33"), first.index("a-mail 2 approvals"))
        self.assertLess(first.index("a-mail 2 approvals"), first.index("changelog-drafter"))

    def test_fleet_http_and_hub_merge_without_loops_or_duplicate_sessions(self):
        self.service = self.service_for()
        self.service.model["machine"] = "laptop"
        due = time.time() + 1200
        self.service.model["later"].append(dict(title="Wake soon", when=due, source="reminders", kind="reminder",
                                                 detail="", host=None, sid=None))
        self.service.model["needs"].append(dict(self.m.need_item("later", "Wake soon", hint="Open the reminder."),
                                                when=due, source="reminders"))
        self.service.model["needs"].append(self.m.need_item("approval", "Confirm a lever", hint="Review in atc."))
        self.service.model["needs"].append(self.m.need_item("approval", "Nested host", host="server2"))
        self.url = self.start(self.service)
        remote = self.m.RemoteAtc(self.url, "fixture-token", background=False)

        remote.fetch()
        hub = self.service_for(self.m.Fleet(only="/no-sessions-here"), [remote])
        model = hub.web_model()
        exported = hub.get("/api/fleet")[1]

        self.assertIsNone(remote.error)
        self.assertEqual(len(remote.view(time.time())["needs"]), 9)
        self.assertEqual(len(model["needs"]), 9)
        self.assertTrue(all(n["host"] == "laptop" for n in model["needs"]))
        self.assertEqual(sum(n["kind"] == "blocked" for n in model["needs"]), 1)
        self.assertEqual(sum(n["kind"] == "waiting" for n in model["needs"]), 1)
        self.assertEqual((model["counts"]["needs"], model["counts"]["needs_urgent"]), (9, 5))
        self.assertEqual(exported["needs"], [])
        self.assertEqual(exported["counts"]["needs"], 0)
        self.assertNotIn("Nested host", json.dumps(remote.data))

        ssh = self.m.Remote("server1", background=False)
        with mock.patch.object(self.m, "feed_and_run", return_value=(0, json.dumps(remote.data).encode(), b"")):
            ssh.fetch()
        self.assertEqual(ssh.view(time.time())["needs"], remote.data["needs"])
        duplicate = self.model([remote, ssh])
        self.assertEqual(len(duplicate["hosts"]), 1)
        self.assertEqual(len(duplicate["needs"]), 16)

    def test_global_waiting_order_and_stale_items(self):
        remote = self.m.Remote("laptop", background=False)
        waiting = dict(self.model()["groups"]["waiting"][0], sid="remote-wait", name="remote-wait", since=1)
        remote.data = {"machine": "laptop", "groups": {"waiting": [waiting]}, "needs": [
            self.m.need_item("amail_request", "a-mail 1 request"),
            self.m.need_item("waiting", "duplicate", sid="remote-wait"),
            self.m.need_item("later", "must not loop", host="server2")]}
        remote.ok_at = time.time() - 10
        remote.error = "offline"

        model = self.model([remote])

        waiting_items = [n for n in model["needs"] if n["kind"] == "waiting"]
        self.assertEqual([n["sid"] for n in waiting_items], ["remote-wait", self.fx.sid("beta-22")])
        self.assertIn("Last seen", waiting_items[0]["hint"])
        self.assertIn("isn't answering", waiting_items[0]["hint"])
        self.assertNotIn("must not loop", json.dumps(model["needs"]))
        self.assertEqual(model["counts"]["needs"], 9)

    def test_web_model_json_requires_session_and_carries_queue(self):
        self.service = self.service_for()
        self.url = self.start(self.service)
        _, cookie = self.enroll(Passkey(self.fx.root))

        status, model, _ = self.request("/api/web/model", cookie=cookie)

        self.assertEqual(status, 200)
        self.assertEqual(model["needs"], self.service.model["needs"])
        self.assertEqual(model["counts"]["needs"], 7)
        self.assertEqual(self.request("/api/web/model")[0], 401)

    def test_json_once_and_mcp_stdio(self):
        data = json.loads(self.cli("--json"))
        once = self.cli("--once", "--width", "200")
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "atc_needs_me", "arguments": {}}}
        response = subprocess.run([sys.executable, fixture.ATC, "mcp", "--no-hosts"], env=self.env,
                                  input=json.dumps(request) + "\n", capture_output=True, text=True, timeout=30)

        self.assertEqual((data["counts"]["needs"], data["counts"]["needs_urgent"]), (7, 4))
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertIn("a-mail 2 approvals", response.stdout)
        self.assertIn("a-mail 2 approvals", once.splitlines()[0])
        self.assertLess(once.index("Waiting on you"), once.index("── a-mail"))
        self.assertIn("Roost or the a-mail app", once)
        disabled = json.loads(self.cli("--json", ATC_AMAIL=""))
        self.assertEqual([n["kind"] for n in disabled["needs"]], ["blocked", "waiting"])

    def test_web_renders_queue_in_order_and_unknown_kinds_as_text(self):
        if not shutil.which("node"):
            self.skipTest("node is needed to exercise the browser rendering functions")
        model = self.service_for().web_model()
        model["needs"].insert(1, self.m.need_item("future_kind", "<script>example</script>",
                                               "Future detail", hint="Future action"))
        script = Path(__file__).with_name("needs_web.js")

        result = subprocess.run(["node", str(script)], input=json.dumps(model), env=self.node_env,
                                capture_output=True, text=True, timeout=10)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("queue rendering passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
