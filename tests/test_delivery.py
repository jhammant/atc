"""Concurrent deliveries use fixture state and the real terminal text/Enter sequence, never a live agent."""
import concurrent.futures
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


class Delivery(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.AmailFixture()
        self.addCleanup(self.fx.stop)
        self.env = self.fx.env()
        patch = mock.patch.dict(os.environ, self.env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.m = load_atc(self.env)
        feed = self.m.Feed([self.env["ATC_CLAUDE"]], background=False)
        feed.fetch()
        self.fleet = self.m.Fleet(feed=feed)
        self.fleet.refresh(time.time())
        self.views = {v["name"]: v for v in self.m.ordered_sessions(self.fleet.model(time.time()), True)}
        self.views["beta-22"]["tty"] = "/dev/ttys100"
        self.views["alpha-11"]["tty"] = "/dev/ttys101"

    def block(self, sid):
        path = Path(self.fx.stub, "agents.json")
        rows = json.loads(path.read_text())
        for row in rows:
            if row["sessionId"] == sid:
                row["status"] = "waiting"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows))
        tmp.replace(path)

    def concurrent(self, *, blocked=False, herdr=False, parallel=False, alias=False):
        m = self.m
        first = dict(self.views["beta-22"])
        second = dict(self.views["alpha-11"] if parallel or alias else first)
        if alias:
            second["tty"] = first["tty"]
        if herdr:
            first.update(kind="herdr", herdr="w1:p1")
            second.update(kind="herdr", herdr="w1:p1")
        # Separate fleet and terminal objects represent separate callers sharing a target.
        other = m.Fleet(feed=m.Feed([self.env["ATC_CLAUDE"]], background=False))
        events = []
        herdr_status = ["idle"]
        first_text, second_text, second_started, release = (threading.Event() for _ in range(4))
        real_run = m.run

        def typed(target, text):
            events.append((target, text))
            if text == "first":
                first_text.set()
                if not release.wait(3):
                    raise AssertionError("first delivery was not released")
            elif text == "second":
                second_text.set()

        def enter(target):
            events.append((target, "Enter"))
            if blocked:
                if herdr:
                    herdr_status[0] = "blocked"
                else:
                    self.block(second["sid"])

        def run(args, **kwargs):
            if args[:2] == ["tmux", "send-keys"]:
                target = args[args.index("-t") + 1]
                if "-l" in args:
                    typed(target, args[-1])
                else:
                    enter(target)
                return 0, "", ""
            return real_run(args, **kwargs)

        def prompt(pane, action, text):
            typed(pane, text)
            enter(pane)
            return "type: done"

        def send_second():
            second_started.set()
            return m.deliver(other, m.Terminals(), second, "second")

        self.fleet.herdr = other.herdr = mock.Mock(act=prompt) if herdr else None
        if herdr:
            self.fleet.herdr.status_now.side_effect = lambda pane: herdr_status[0]
            self.fleet.feed.status_now = other.feed.status_now = mock.Mock(side_effect=AssertionError("Claude feed used for Herdr"))
        with mock.patch.object(m, "run", side_effect=run):
            with mock.patch.object(m.Terminals, "locate", side_effect=lambda tty: ("tmux", tty)):
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    a = pool.submit(m.deliver, self.fleet, m.Terminals(), first, "first")
                    try:
                        self.assertTrue(first_text.wait(2))
                        b = pool.submit(send_second)
                        self.assertTrue(second_started.wait(1))
                        self.assertEqual(second_text.wait(0.5), parallel)
                    finally:
                        release.set()
                    results = a.result(timeout=3), b.result(timeout=3)

        self.assertTrue(results[0][0], results)
        if herdr:
            self.assertEqual(self.fleet.herdr.status_now.call_args_list, [mock.call("w1:p1"), mock.call("w1:p1")])
        if blocked:
            self.assertFalse(results[1][0], results)
            self.assertIn("blocked", results[1][1])
            self.assertEqual([text for _, text in events], ["first", "Enter"])
        elif parallel:
            self.assertTrue(results[1][0], results)
            for target, text in ((first["tty"], "first"), (second["tty"], "second")):
                self.assertEqual([value for key, value in events if key == target], [text, "Enter"])
        else:
            self.assertTrue(results[1][0], results)
            self.assertEqual([text for _, text in events], ["first", "Enter", "second", "Enter"])

    def test_same_tty_text_and_enter_never_interleave(self):
        self.concurrent()

    def test_same_tty_with_different_session_ids_uses_one_lock(self):
        self.concurrent(alias=True)

    def test_waiter_rechecks_state_after_previous_enter(self):
        self.concurrent(blocked=True)

    def test_other_sessions_still_deliver_in_parallel(self):
        self.concurrent(parallel=True)

    def test_herdr_pane_serializes_and_rechecks_state(self):
        for blocked in (False, True):
            with self.subTest(blocked=blocked):
                self.concurrent(herdr=True, blocked=blocked)

    def test_failed_delivery_releases_lock(self):
        v = self.views["beta-22"]
        terminal = mock.Mock()
        terminal.act.side_effect = [OSError("terminal disconnected"), "type: done"]
        with self.assertRaisesRegex(OSError, "terminal disconnected"):
            self.m.deliver(self.fleet, terminal, v, "first")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(self.m.deliver, self.fleet, terminal, v, "second").result(timeout=2)
        self.assertTrue(result[0])

    def test_separate_processes_share_the_target_lock(self):
        script = '''
import importlib.util, json, os, sys, time
from pathlib import Path
from types import SimpleNamespace
spec = importlib.util.spec_from_file_location("atc_delivery", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
root, text = Path(sys.argv[2]), sys.argv[3]
v = json.loads((root / "target.json").read_text())
fleet = SimpleNamespace(feed=m.Feed([os.environ["ATC_CLAUDE"]], background=False), herdr=None)
class Terminal:
    def act(self, tty, action, message):
        with (root / "events").open("a") as fh:
            fh.write(message + "\\n")
        if message == "first":
            until = time.monotonic() + 5
            while not (root / "release").exists():
                if time.monotonic() >= until:
                    raise RuntimeError("release timed out")
                time.sleep(0.01)
        with (root / "events").open("a") as fh:
            fh.write("Enter\\n")
        return "type: done"
(root / ("started-" + text)).touch()
assert m.deliver(fleet, Terminal(), v, text)[0]
'''
        root = Path(self.fx.root)
        (root / "target.json").write_text(json.dumps(self.views["beta-22"]))
        processes = []

        def start(text):
            process = subprocess.Popen([sys.executable, "-c", script, fixture.ATC, str(root), text],
                                       env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            processes.append(process)

        def wait_for(predicate):
            until = time.monotonic() + 3
            while not predicate() and time.monotonic() < until:
                time.sleep(0.01)
            self.assertTrue(predicate())

        try:
            start("first")
            wait_for(lambda: (root / "events").exists())
            start("second")
            wait_for(lambda: (root / "started-second").exists())
            time.sleep(0.3)
            self.assertEqual((root / "events").read_text().splitlines(), ["first"])
        finally:
            (root / "release").touch()
            for process in processes:
                out, err = process.communicate(timeout=5)
                self.assertEqual((process.returncode, out, err), (0, "", ""))
        self.assertEqual((root / "events").read_text().splitlines(), ["first", "Enter", "second", "Enter"])
