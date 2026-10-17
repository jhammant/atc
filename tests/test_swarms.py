"""Route swarm snapshots, incremental a-mail, model and CLI/TUI views.

Run from the repo root: python3 -m unittest discover -s tests -v
"""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixture  # noqa: E402


def text(lines):
    return "\n".join("".join(part for part, _style in line) for line in lines)


class Swarms(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.SwarmFixture()
        self.addCleanup(self.fx.stop)
        env = mock.patch.dict(os.environ, self.fx.env(), clear=True)
        env.start()
        self.addCleanup(env.stop)
        spec = importlib.util.spec_from_file_location("atc_swarm_test", fixture.ATC)
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.fleet = self.m.Fleet()
        self.refresh()

    def refresh(self, now=None):
        now = self.fx.now if now is None else now
        children = {}
        for pid, proc in self.fx.table.items():
            children.setdefault(proc["ppid"], []).append(pid)
        self.fleet.proc_watch.snapshot = (self.fx.table, children, now)
        self.fleet.refresh(now)
        self.model = self.fleet.model(now)
        self.views = {v["id"]: v for v in self.model["swarms"]}

    def atc(self, *args):
        result = subprocess.run([sys.executable, fixture.ATC, "--no-hosts", *args], env=self.fx.env(),
                                cwd=self.fx.work, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def status(self, ident="live"):
        return json.loads(Path(self.fx.status_path(ident)).read_text())

    def state_snapshot(self):
        return {str(p.relative_to(self.fx.state)): (p.stat().st_mtime_ns, p.stat().st_mode,
                                                   p.read_bytes() if p.is_file() else None)
                for p in Path(self.fx.state).rglob("*")}

    def test_model_retains_running_recent_and_stale_but_expires_finished(self):
        self.assertEqual(set(self.views), {"live", "recent", "stale"})
        self.assertEqual(self.model["counts"]["swarms"], 1)
        self.assertEqual(self.model["counts"]["swarm_units_running"], 2)
        live = self.views["live"]
        self.assertEqual(live["state"], "running")
        self.assertFalse(live["stale"])
        self.assertEqual(live["age"], 600)
        self.assertEqual(live["messages"], 2)
        self.assertEqual(live["held"][0]["id"], "needs-context")
        self.assertEqual(live["last_message"], {"from": "?stray.worker", "text": "Need the shared fixture",
                                               "at": self.fx.now - 5})
        self.assertEqual(self.views["recent"]["exit_code"], 0)
        self.assertEqual(self.views["stale"]["state"], "stopped?")
        self.assertTrue(self.views["stale"]["stale"])
        self.refresh(self.fx.now + 7 * 3600)
        self.assertEqual(set(self.views), {"live", "stale"})

    def test_state_directory_precedence(self):
        with mock.patch.dict(os.environ, {"ROUTE_STATE_DIR": "/explicit", "XDG_STATE_HOME": "/xdg"}, clear=True):
            self.assertEqual(self.m.route_state_dir(), "/explicit")
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/xdg"}, clear=True):
            self.assertEqual(self.m.route_state_dir(), "/xdg/route")
        with mock.patch.dict(os.environ, {"HOME": self.fx.root}, clear=True):
            self.assertEqual(self.m.route_state_dir(), self.fx.root + "/.local/state/route")

    def test_retention_window_is_configurable(self):
        with mock.patch.dict(os.environ, {"ATC_SWARM_HOURS": "0.5"}):
            self.fleet.swarms = self.m.Swarms()
        self.refresh()
        self.assertEqual(set(self.views), {"live", "stale"})
        for value in ("bad", "nan", "inf", "-2"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"ATC_SWARM_HOURS": value}):
                self.assertEqual(self.m.Swarms().window, 6 * 3600)

    def test_owner_follows_parent_chain_and_stops_at_nearest_session(self):
        self.assertEqual(self.views["live"]["owner"], "owner-session")
        self.assertIsNone(self.views["stale"]["owner"])
        nested = self.m.Session("nested", self.fx.work)
        nested.pid, nested.entry = 110, {"name": "nearer-owner"}
        sessions = {**self.fleet.sessions, "nested": nested}
        self.assertEqual(self.m.swarm_owner(120, self.fx.table, sessions), "nearer-owner")
        cycle = {10: {"ppid": 11}, 11: {"ppid": 10}}
        self.assertIsNone(self.m.swarm_owner(10, cycle, sessions))

    def test_owner_by_recorded_socket_survives_route_exiting(self):
        # Arrange: route has exited (no pid chain left), but it recorded its session's socket.
        addresses = {"uds:/tmp/cc-socks/4242.sock": "socket-owner"}
        # Act
        by_socket = self.m.swarm_owner(999999, {}, self.fleet.sessions, "/tmp/cc-socks/4242.sock", addresses)
        unknown = self.m.swarm_owner(999999, {}, self.fleet.sessions, "/tmp/cc-socks/1.sock", addresses)
        # Assert
        self.assertEqual(by_socket, "socket-owner")
        self.assertIsNone(unknown)

    def test_route_liveness_rejects_missing_and_reused_pids(self):
        for command in ("/usr/bin/python3 unrelated.py", "sleep 300", "node /tools/route.js"):
            with self.subTest(command=command):
                self.fx.table[120]["command"] = command
                self.refresh()
                self.assertEqual(self.views["live"]["state"], "stopped?")
                self.assertEqual(self.model["counts"]["swarms"], 0)
        for command in ("/usr/bin/python3 /tools/route.py swarm", "/bin/python3.14 /tools/auto swarm",
                        "/Library/Python -m route swarm"):
            with self.subTest(command=command):
                self.fx.table[120]["command"] = command
                self.refresh()
                self.assertEqual(self.views["live"]["state"], "running")
        del self.fx.table[120]
        self.refresh()
        self.assertEqual(self.views["live"]["state"], "stopped?")

    def test_units_are_named_workers_and_not_duplicated(self):
        live = self.views["live"]
        units = {u["id"]: u for u in live["units"]}
        self.assertEqual(units["lost"]["state"], "stopped?")
        self.assertEqual(units["complete"]["wall"], 60)
        self.assertEqual([a["pid"] for a in self.model["agents"]], [400])
        owner = self.m.ordered_sessions(self.model, True)[0]
        self.assertEqual({w["label"] for w in owner["workers"]}, {"release/alpha", "release/beta"})
        self.assertEqual(len(owner["workers"]), 2)  # includes no nested worker or duplicate
        rendered = text(line for line, _key in self.m.fleet_lines(self.model, 160, True))
        self.assertNotIn("codex exec implement", rendered)
        self.assertNotIn("nested-worker", rendered)
        self.fx.table[120]["ppid"] = 1  # an unowned swarm is still not a loose agent
        self.refresh()
        self.assertIsNone(self.views["live"]["owner"])
        self.assertEqual([a["pid"] for a in self.model["agents"]], [400])
        tree = text(self.m.tree_lines(self.fleet, self.model))
        self.assertIn("route swarms\n├─ swarm release (running)", tree)
        self.assertEqual(tree.count("process codex exec implement"), 1)

    def test_only_filters_swarms_by_repo(self):
        self.fleet.only = self.fx.work
        self.refresh()
        self.assertEqual(set(self.views), {"live", "recent", "stale"})
        self.fleet.only = self.fx.work + "/different"
        self.refresh()
        self.assertEqual(self.model["swarms"], [])
        self.assertEqual(self.model["comms"], [])

    def test_comms_keeps_full_text_and_unknown_senders(self):
        items = [c for c in self.model["comms"] if c["kind"] == "swarm"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["from"], "release/alpha")
        self.assertEqual(items[0]["to"], "release/hub")
        self.assertEqual(items[0]["summary"], "Implementation ready")
        self.assertEqual(items[0]["body"], "Implementation ready\nAll checks passed.")
        self.assertEqual(items[1]["from"], "release/?stray.worker")
        self.assertIn("⇄", text(self.m.comm_lines(items, 10)))

    def test_status_is_read_only_when_mtime_changes_including_expired_files(self):
        swarms = self.m.Swarms()
        with mock.patch("builtins.open", wraps=open) as opened:
            swarms.refresh(self.fx.now)

            def reads():
                return [c for c in opened.call_args_list if str(c.args[0]).endswith("status.json")]

            self.assertEqual(len(reads()), 4)
            swarms.refresh(self.fx.now + 1)
            self.assertEqual(len(reads()), 4)
            data = self.status()
            data["name"] = "renamed"
            self.fx.write_status(data)
            before = len(reads())
            swarms.refresh(self.fx.now + 2)
            self.assertEqual(len(reads()), before + 1)
        self.assertEqual(next(e["status"]["name"] for e in swarms.entries if e["status"]["swarm_id"] == "live"), "renamed")

    def test_channel_is_tailed_incrementally_and_bounded_to_200(self):
        with open(self.fx.channel_path(), "a") as fh:
            for n in range(210):
                fh.write(json.dumps({"at": None, "from": "alpha@hub", "from_unit": "alpha", "text": f"message {n}"}) + "\n")
        self.refresh()
        items = [c for c in self.model["comms"] if c["kind"] == "swarm"]
        self.assertEqual(len(items), 200)
        self.assertEqual(items[0]["body"], "message 10")
        self.assertEqual(items[-1]["body"], "message 209")
        self.assertEqual(self.views["live"]["messages"], 212)
        self.refresh()
        self.assertEqual(self.model["comms"], items)
        with open(self.fx.channel_path(), "a") as fh:
            fh.write('{"at":null,"from":"alpha@hub","from_unit":"alpha","text":"partial"')
        self.refresh()
        self.assertEqual(self.views["live"]["last_message"]["text"], "message 209")
        with open(self.fx.channel_path(), "a") as fh:
            fh.write("}\n")
        self.refresh()
        self.assertEqual(self.views["live"]["last_message"]["text"], "partial")
        self.assertEqual(len(self.model["comms"]), 200)

    def test_truncated_channel_restarts_and_bad_records_are_skipped(self):
        fixture.write_jsonl(self.fx.channel_path(), [{"at": None, "from": "x@hub", "from_unit": None, "text": "reset"}])
        self.refresh()
        self.assertEqual(len(self.model["comms"]), 1)
        with open(self.fx.channel_path(), "a") as fh:
            for rec in ({"from": "x", "text": {}}, {"from": 1, "text": "bad"},
                        {"from": "x", "text": "bad", "from_unit": []}, {"from": "x", "text": "bad", "at": float("nan")}, []):
                fh.write(json.dumps(rec) + "\n")
            fh.write("broken json\n")
        self.refresh()
        self.assertEqual(len(self.model["comms"]), 1)
        self.assertEqual(self.views["live"]["last_message"]["from"], "?x")

    def test_malformed_status_and_wrong_schema_are_counted_and_skipped(self):
        base = self.status()
        bad = [[], {**base, "schema": "route-swarm-status/2"}, {**base, "units": {}},
               {**base, "state": "bogus"}, {**base, "started_at": "bad"}, {**base, "route_pid": "120"},
               {**base, "finished_at": []}, {**base, "channel": {"messages": "2"}}, {**base, "held": [None]},
               {**base, "units": [{**base["units"][0], "state": []}]},
               {**base, "units": [{**base["units"][0], "wall_clock_s": float("inf")}]},
               {**base, "units": [{**base["units"][0], "wall_clock_s": 10 ** 1000}]}]
        for i, data in enumerate(bad):
            self.fx.write_json(self.fx.status_path(f"invalid-{i}"), data)
        path = Path(self.fx.status_path("malformed"))
        path.parent.mkdir()
        path.write_text("{")
        self.refresh()
        self.assertEqual(set(self.views), {"live", "recent", "stale"})
        self.assertEqual(self.model["swarms_unreadable"], len(bad) + 1)
        self.refresh()
        self.assertEqual(self.model["swarms_unreadable"], len(bad) + 1)
        path.unlink()
        self.refresh()
        self.assertEqual(self.model["swarms_unreadable"], len(bad))

    def test_unreadable_status_and_unknown_fields(self):
        real_open = open

        def denied(path, *args, **kwargs):
            if str(path) == self.fx.status_path():
                raise PermissionError("fixture denied status read")
            return real_open(path, *args, **kwargs)

        self.fleet.swarms = self.m.Swarms()
        with mock.patch("builtins.open", side_effect=denied):
            self.refresh()
        self.assertEqual(self.model["swarms_unreadable"], 1)
        self.assertNotIn("live", self.views)
        data = self.status()
        data["future_extension"] = {"anything": True}
        data["units"][0]["future_extension"] = []
        self.fx.write_status(data)
        self.refresh()
        self.assertEqual(self.model["swarms_unreadable"], 0)
        self.assertIn("live", self.views)

    def test_disappearing_swarm_is_removed(self):
        Path(self.fx.status_path()).unlink()
        self.refresh()
        self.assertNotIn("live", self.views)
        self.assertEqual(self.model["comms"], [])

    def test_fleet_section_order_marks_colors_and_finished_toggle(self):
        rows = self.m.fleet_lines(self.model, 180, True)
        rendered = text(line for line, _key in rows)
        self.assertLess(rendered.index("Working (1)"), rendered.index("Swarms (3)"))
        self.assertLess(rendered.index("Swarms (3)"), rendered.index("Other agents (1)"))
        self.assertIn("route swarm · a-mail", rendered)
        for label in ("● alpha", "● beta", "◐ gamma", "○ later", "✓ complete", "✗ broken", "? lost",
                      "?stray.worker → hub: Need the shared fixture", "2 msgs"):
            self.assertIn(label, rendered)
        colors = {style for line, _key in rows for part, style in line if part in ("codex", "kimi", "claude")}
        self.assertEqual(len(colors), 3)
        folded = text(line for line, _key in self.m.fleet_lines(self.model, 180, False))
        self.assertIn("Swarms (2)", folded)
        self.assertIn("release", folded)
        self.assertIn("abandoned", folded)
        self.assertNotIn("finished", folded)
        for width in (1, 40, 80, 160):
            frame, _keys = self.m.render(self.fleet, self.model, {"selected": None, "show_all": True}, width)
            self.assertTrue(all(self.m.seg_len(line) <= width for line in frame))

    def test_tree_nests_swarm_unit_and_worker_once(self):
        tree = text(self.m.tree_lines(self.fleet, self.model))
        self.assertIn("owner-session", tree)
        self.assertIn("swarm release (running)", tree)
        self.assertIn("alpha (codex, running)", tree)
        self.assertEqual(tree.count("process codex exec implement"), 1)
        lines = tree.splitlines()
        unit = next(i for i, line in enumerate(lines) if "alpha (codex, running)" in line)
        self.assertIn("process codex exec implement", lines[unit + 1])
        self.assertGreater(lines[unit + 1].index("process"), lines[unit].index("alpha"))
        self.assertIn("route swarms\n└─ swarm abandoned (stopped?)", tree)
        self.assertNotIn("nested-worker", tree)
        mine = text(self.m.tree_lines(self.fleet, self.model, self.fx.sid))
        self.assertIn("swarm release", mine)
        self.assertNotIn("abandoned", mine)

    def test_selection_details_and_actions_are_read_only(self):
        app = self.m.App(self.fleet, self.m.Quota(None, background=False), self.m.Terminals(), None, ["claude"], False)
        for key in ("swarm::live", "swarm::live::alpha"):
            with self.subTest(key=key):
                item = self.m.resolve(self.fleet, self.model, key)
                self.assertEqual(item[0], "swarm")
                detail = text(self.m.selection_lines(self.fleet, self.model, item, 0, 200))
                for unit in self.views["live"]["units"]:
                    for field in ("id", "log", "branch", "worktree"):
                        self.assertIn(unit[field], detail)
                self.assertIn("held needs-context", detail)
                app.ui.update(selected=key, view=None)
                with mock.patch.object(self.m, "run") as run, mock.patch.object(self.m, "stop_process") as stop:
                    for action in ("m", "M", "x", "x", "C", "C", "F", "F", "E", "S", "N"):
                        app.handle(action, self.model)
                    run.assert_not_called()
                    stop.assert_not_called()
                app.handle("\n", self.model)
                self.assertEqual(app.ui["view"], "detail")
                frame = self.m.render_view(self.fleet, self.model, app.ui, 160, 40)
                self.assertIn("read-only", text(frame))
        self.assertEqual(self.m.resolve(self.fleet, self.model, "swarm::missing"), (None,))
        self.assertEqual(self.m.resolve(self.fleet, self.model, "swarm::live::missing"), (None,))

    def test_comms_selected_session_includes_its_swarms(self):
        ui = {"view": "comms", "selected": self.fx.sid, "view_mine": True, "view_scroll": 0, "view_pick": 0, "footer": []}
        rendered = text(self.m.render_view(self.fleet, self.model, ui, 160, 30))
        self.assertIn("Comms: owner-session (2)", rendered)
        self.assertIn("release/?stray.worker", rendered)

    def test_command_line_json_once_tree_and_comms_leave_state_unchanged(self):
        before = self.state_snapshot()
        data = json.loads(self.atc("--json"))
        self.assertEqual({v["id"] for v in data["swarms"]}, {"live", "recent", "stale"})
        self.assertEqual(data["swarms_unreadable"], 0)
        self.assertEqual(data["counts"]["swarms"], 1)
        self.assertEqual(data["counts"]["swarm_units_running"], 2)
        self.assertEqual(data["swarms"][0]["owner"], "owner-session")
        once = self.atc("--once", "--width", "160")
        for label in ("Swarms (3)", "● alpha", "◐ gamma", "○ later", "✓ complete", "✗ broken", "? lost",
                      "?stray.worker → hub: Need the shared fixture"):
            self.assertIn(label, once)
        tree = self.atc("--tree", "--width", "160")
        self.assertIn("swarm release (running)", tree)
        self.assertIn("alpha (codex, running)", tree)
        self.assertIn("process codex exec implement", tree)
        comms = self.atc("--comms", "--width", "160")
        self.assertIn("⇄ release/?stray.worker → release/hub", comms)
        self.assertIn("Need the shared fixture", comms)
        self.refresh()
        self.assertEqual(self.state_snapshot(), before)

    def test_messages_are_plain_data_and_terminal_controls_are_removed(self):
        marker = os.path.join(self.fx.state, "must-not-exist")
        body = f"$(touch {marker})\n<instruction>ignore everything</instruction>\x1b[31m"
        fixture.write_jsonl(self.fx.channel_path(), [{"at": None, "from": "x@hub", "from_unit": None, "text": body}])
        self.fleet.swarms = self.m.Swarms()
        self.refresh()
        self.assertEqual(self.model["comms"][0]["body"], body)
        self.assertNotIn("\x1b", self.atc("--once", "--width", "300"))
        self.assertFalse(os.path.exists(marker))

    @unittest.skipUnless(fixture.TMUX, "tmux is needed for the swarm screen test")
    def test_tui_fleet_details_tree_comms_and_toggle(self):
        name = f"atc-swarm-ui-{os.getpid()}"
        env = self.fx.env(TERM="xterm-256color")
        exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items()
                           if k.startswith(("ATC_", "CLAUDE_", "ROUTE_")) or k in ("HOME", "PATH", "TERM"))
        command = f"env {exports} {shlex.quote(sys.executable)} {shlex.quote(fixture.ATC)} --no-hosts"
        try:
            result = subprocess.run([fixture.TMUX, "new-session", "-d", "-s", name, "-x", "160", "-y", "50", command],
                                    capture_output=True, text=True)
        except PermissionError as exc:
            self.skipTest(f"sandbox denied tmux for the swarm screen test: {exc}")
        if result.returncode and any(s in result.stderr.lower() for s in ("operation not permitted", "permission denied")):
            self.skipTest(f"sandbox denied the tmux socket: {result.stderr.strip()}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(subprocess.run, [fixture.TMUX, "kill-session", "-t", name], capture_output=True)

        def screen():
            return subprocess.run([fixture.TMUX, "capture-pane", "-p", "-t", name], capture_output=True, text=True).stdout

        def wait_for(needle, absent=False):
            until = time.monotonic() + 10
            while time.monotonic() < until:
                output = screen()
                if (needle in output) != absent:
                    return output
                time.sleep(0.1)
            self.fail(f"screen did not {'hide' if absent else 'show'} {needle!r}:\n{screen()}")

        def press(key):
            subprocess.run([fixture.TMUX, "send-keys", "-t", name, key], check=True, capture_output=True)

        before = self.state_snapshot()
        wait_for("Swarms (3)")
        wait_for("?stray.worker → hub: Need the shared fixture")
        press("Down")
        wait_for("▶ ● release")
        press("Enter")
        wait_for("read-only")
        wait_for("branch: swarm/release/alpha")
        press("Escape")
        wait_for("Swarms (3)")
        press("Down")
        wait_for("● alpha")
        press("Enter")
        wait_for("── release/alpha")
        press("Escape")
        wait_for("Swarms (3)")
        press("c")
        wait_for("⇄ release/?stray.worker")
        press("h")
        wait_for("swarm release (running)")
        wait_for("alpha (codex, running)")
        wait_for("process codex exec implement")
        press("Escape")
        wait_for("Swarms (3)")
        press("p")
        wait_for("Swarms (2)")
        wait_for("✓ finished", absent=True)
        wait_for("? abandoned")
        self.assertEqual(self.state_snapshot(), before)
        press("q")


if __name__ == "__main__":
    unittest.main()
