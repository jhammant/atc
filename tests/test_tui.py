"""Screen tests: atc runs in tmux against the fake Claude home, keys are pressed, and the screen is read back
exactly as drawn. Steering is checked in the dummy sessions' own terminals.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import os
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixture  # noqa: E402
from test_units import load_atc  # noqa: E402

FX = None
TMUX = fixture.TMUX
LIVE_SKIP = ""


def setUpModule():
    global FX, LIVE_SKIP
    if not TMUX:
        LIVE_SKIP = "tmux is needed for the screen tests"
        return
    try:
        FX = fixture.Fixture().start()
    except unittest.SkipTest as exc:
        LIVE_SKIP = str(exc)


def tearDownModule():
    if FX:
        FX.stop()


class Screen:
    """atc running in a tmux window of its own, with keys in and the screen out."""

    def __init__(self, width, height, *args, hosts=False, extra_env=None, ready="Blocked on you", fx=None):
        fx = fx or FX
        self.name = f"atc-ui-{os.getpid()}-{width}x{height}"
        self.err = os.path.join(fx.root, f"{self.name}.err")
        self.code = os.path.join(fx.root, f"{self.name}.code")
        self.debug = os.path.join(fx.root, f"{self.name}.debug")
        env = fx.env()
        env.update(ATC_DEBUG_LOG=self.debug, **(extra_env or {}))
        exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items()
                           if k.startswith(("ATC_", "CLAUDE_", "ROUTE_", "TERM")) or k == "PATH")
        hosts_arg = "" if hosts else "--no-hosts"
        command = (f"cd {shlex.quote(fx.work)} && env {exports} HOME={shlex.quote(env['HOME'])} "
                   f"{shlex.quote(sys.executable)} {shlex.quote(fixture.ATC)} {hosts_arg} {' '.join(args)} "
                   f"2> {shlex.quote(self.err)}; echo $? > {shlex.quote(self.code)}; sleep 30")
        subprocess.run([TMUX, "new-session", "-d", "-s", self.name, "-x", str(width), "-y", str(height), command],
                       check=True)
        try:
            self.wait_for(ready)
        except AssertionError:  # don't leave a session running when a test can't start
            subprocess.run([TMUX, "kill-session", "-t", self.name], capture_output=True)
            raise

    def text(self):
        return subprocess.run([TMUX, "capture-pane", "-p", "-t", self.name], capture_output=True, text=True).stdout

    def lines(self):
        return self.text().splitlines()

    def press(self, *keys, literal=False, pause=0.4):
        for key in keys:
            subprocess.run([TMUX, "send-keys", "-t", self.name] + (["-l"] if literal else []) + [key], check=True)
            time.sleep(0.08)
        time.sleep(pause)

    def type(self, text):
        self.press(text, literal=True, pause=0.2)

    def wait_for(self, needle, timeout=8.0, absent=False):
        end = time.time() + timeout
        while time.time() < end:
            present = needle in self.text()
            if present != absent:
                return
            time.sleep(0.1)
        raise AssertionError(f"{'still saw' if absent else 'never saw'} {needle!r} on screen:\n{self.text()}")

    def selected(self):
        line = self.selected_line()
        return line.split()[1] if line and len(line.split()) > 1 else None

    def selected_line(self):
        return next((line[1:] for line in self.lines() if line.startswith("▶")), "")

    def select_line(self, needle, tries=40):
        """Arrow down from the top until the selected row contains `needle`."""
        self.select("gamma-33")
        for _ in range(tries):
            if needle in self.selected_line():
                return
            before = self.selected_line()
            self.step("Down")
            if self.selected_line() == before:
                break
        raise AssertionError(f"no row with {needle!r}; selected: {self.selected_line()!r}")

    def step(self, key):
        """Press one arrow and wait until the screen shows the selection has moved (or can't move)."""
        before = self.selected_line()
        self.press(key, pause=0.05)
        end = time.time() + 1.0
        while time.time() < end and self.selected_line() == before:
            time.sleep(0.05)

    def select(self, name):
        for key in ("Up", "Down"):
            for _ in range(60):
                if self.selected() == name or name in self.selected_line():
                    return
                before = self.selected_line()
                self.step(key)
                if self.selected_line() == before:
                    break  # reached the end in this direction
        if self.selected() != name and name not in self.selected_line():
            raise AssertionError(f"couldn't select {name}; selected is {self.selected()}")

    def footer(self):
        return self.lines()[-1] if self.lines() else ""

    def quit(self):
        self.press("q", pause=0.5)
        end = time.time() + 5
        while time.time() < end and not os.path.exists(self.code):
            time.sleep(0.1)
        with open(self.code) as fh:
            code = fh.read().strip()
        with open(self.err) as fh:
            err = fh.read()
        subprocess.run([TMUX, "kill-session", "-t", self.name], capture_output=True)
        return code, err


class Wide(unittest.TestCase):
    """160 columns: activity and comms side by side."""

    @classmethod
    def setUpClass(cls):
        if LIVE_SKIP:
            raise unittest.SkipTest(LIVE_SKIP)
        cls.s = Screen(160, 45)

    @classmethod
    def tearDownClass(cls):
        code, err = cls.s.quit()
        assert code == "0" and not err.strip(), (code, err)

    def setUp(self):
        FX.clear_calls()
        if "Hierarchy:" in self.s.text() or "Comms:" in self.s.text() or "── Keys" in self.s.text():
            self.s.press("Escape")

    def test_01_layout(self):
        text = self.s.text()
        for heading in ("Blocked on you (1)", "Waiting on you (1)", "Working (1)", "Parked (1)", "Background (1)",
                        "Activity, all sessions", "Comms"):
            self.assertIn(heading, text)
        self.s.wait_for("unsaved work in 1 repo")
        self.s.wait_for("uncommitted")
        self.assertIn("quota  Claude 5h 12% · week 40%", text)
        for line in self.s.lines():
            self.assertLessEqual(len(line), 160)
            if line.startswith("──"):  # headings start at column 0 and run to the edge
                self.assertTrue(line.rstrip().endswith("──"), line)

    def test_02_arrows_move_the_selection_and_it_stays_put(self):
        s = self.s
        s.select("gamma-33")
        s.step("Down")
        self.assertIn("beta-22", s.selected_line())  # beta-22 shows as changelog-drafter but line has beta-22 dim
        s.step("Down")
        self.assertIn("alpha-11", s.selected_line())
        s.step("Down")
        self.assertIn("API client", s.selected_line())  # into alpha's workflow agents and subagents
        s.step("Up")
        self.assertIn("alpha-11", s.selected_line())
        s.step("Up")
        self.assertIn("beta-22", s.selected_line())
        s.step("k")
        self.assertIn("gamma-33", s.selected_line())
        s.step("j")
        row = next(i for i, line in enumerate(s.lines()) if line.startswith("▶"))
        time.sleep(3)  # several refreshes later: same session, same row
        self.assertIn("beta-22", s.selected_line())
        self.assertEqual(next(i for i, line in enumerate(s.lines()) if line.startswith("▶")), row)

    def test_03_b_switches_the_left_panel(self):
        s = self.s
        s.select("beta-22")
        s.wait_for("── Activity, all sessions")
        s.press("b")
        s.wait_for("── Selected: beta-22")
        self.assertIn("said   Done. Want me to ship it?", s.text())
        s.press("b")
        s.wait_for("── Activity, all sessions")  # two panels here: comms is already on the right

    def test_04_comms_view(self):
        s = self.s
        s.press("c")
        s.wait_for("Comms: all sessions")
        self.assertIn("✉@ alpha-11 → beta-22", s.text())
        first = s.text()
        s.press("Down")
        self.assertNotEqual(s.text(), first)  # another message picked, another body shown
        s.press("s")
        s.wait_for("Comms: ")
        s.press("Escape")
        s.wait_for("Blocked on you")

    def test_05_tree_view(self):
        s = self.s
        s.press("h")
        s.wait_for("Hierarchy: all sessions")
        text = s.text()
        for needle in ("workflow release-train", "phase Build", "phase Verify", "Write the tests", "Lint fixer",
                       "talks with beta-22 ×2"):
            self.assertIn(needle, text)
        s.press("PageDown", "PageUp")
        s.press("Escape")
        s.wait_for("Blocked on you")

    def test_06_help(self):
        self.s.press("?")
        self.s.wait_for("── Keys")
        self.assertIn("interrupt the selected session", self.s.text())
        self.s.press("Escape")
        self.s.wait_for("Blocked on you")

    def test_07_parked_and_background(self):
        s = self.s
        s.wait_for("── Parked (1)")  # sessions that are open but idle are listed by default
        self.assertIn("delta-44", s.text())
        s.press("p")
        s.wait_for("Background (1): bg deadbeef")
        s.press("p")
        s.wait_for("── Background (1)")

    def test_08_swarm_cap(self):
        s = self.s
        s.press("+")
        s.wait_for("swarm cap pinned to abundant")
        s.wait_for("≤8 abundant (pinned)")
        self.assertIn("quotamax override abundant 2", FX.calls())
        s.press("0")
        s.wait_for("swarm cap back on auto")
        s.wait_for("≤4 comfortable (auto)")
        s.press("-")
        s.wait_for("≤2 constrained (pinned)")
        s.press("0")
        s.wait_for("≤4 comfortable (auto)")

    def test_09_new_task_goes_through_route(self):
        s = self.s
        s.press("n")
        s.wait_for("new task in")
        s.type("write tests")
        s.wait_for("write tests_")
        s.press("Enter")
        s.wait_for("→ codex")
        self.assertIn("route task --plan-only write tests", FX.calls())
        s.press("Enter")
        s.wait_for("run this yourself: route task --pool codex 'write tests'")

    def test_10_new_task_cancel(self):
        s = self.s
        s.press("n")
        s.wait_for("new task in")
        s.type("never mind")
        s.press("Escape")
        s.wait_for("new task in", absent=True)
        self.assertNotIn("never mind", FX.calls())

    def test_11_message_a_waiting_session(self):
        s = self.s
        s.select("beta-22")
        s.press("m")
        s.wait_for("message  beta-22:")
        s.type("hello beta")
        s.press("Enter")
        s.wait_for("sent to beta-22")
        deadline = time.time() + 5
        while "hello beta" not in FX.pane("beta-22") and time.time() < deadline:
            time.sleep(0.1)
        self.assertIn("hello beta", FX.pane("beta-22"))

    def test_12_never_types_into_a_blocked_session(self):
        s = self.s
        s.select("gamma-33")
        s.press("m")
        s.type("this must not arrive")
        s.press("Enter")
        s.wait_for("skipped gamma-33")
        time.sleep(1)
        self.assertNotIn("this must not arrive", FX.pane("gamma-33"))

    def test_13_broadcast_to_a_project(self):
        s = self.s
        s.select("beta-22")  # works in the project's root folder, so its project holds all three
        s.press("M")
        s.wait_for("message  3 sessions:")
        s.type("sync up")
        s.press("Enter")
        s.wait_for("skipped gamma-33")
        footer = s.footer()
        self.assertIn("alpha-11", footer)
        self.assertIn("beta-22", footer)
        time.sleep(1)
        self.assertIn("sync up", FX.pane("alpha-11"))
        self.assertIn("sync up", FX.pane("beta-22"))
        self.assertNotIn("sync up", FX.pane("gamma-33"))

    def test_14_interrupt_needs_two_presses(self):
        s = self.s
        s.select("alpha-11")
        before = FX.pane("alpha-11").count("^[")
        s.press("x")
        s.wait_for("press x again")
        self.assertEqual(FX.pane("alpha-11").count("^["), before)
        s.press("x")
        s.wait_for("interrupt: done")
        time.sleep(0.5)
        self.assertEqual(FX.pane("alpha-11").count("^["), before + 1)

    def test_15_enter_jumps_to_the_session(self):
        s = self.s
        s.select("gamma-33")
        s.press("Enter")
        s.wait_for("focus: done")
        self.assertEqual(FX.active_window(), FX.sessions["gamma-33"]["window"])
        s.select("beta-22")
        s.press("Enter")
        s.wait_for("focus: done")
        self.assertEqual(FX.active_window(), FX.sessions["beta-22"]["window"])

    def test_16_held_arrow_keys(self):
        s = self.s
        s.select("gamma-33")
        subprocess.run([TMUX, "send-keys", "-t", s.name] + ["Down"] * 40, check=True)  # a burst, like a held key
        time.sleep(0.8)
        self.assertIn("api-client", s.selected_line())  # the last row (closed session)

    def test_17_resize(self):
        s = self.s
        for width, height in ((70, 18), (220, 60), (160, 45)):
            subprocess.run([TMUX, "resize-window", "-t", s.name, "-x", str(width), "-y", str(height)], check=True)
            time.sleep(0.8)
            text = s.text()
            self.assertIn(" atc ", text)
            for line in text.splitlines():
                self.assertLessEqual(len(line), width)
            clock = s.lines()[1].split()[1]
            time.sleep(2.2)  # still alive: the clock in the header keeps moving after a resize
            self.assertNotEqual(s.lines()[1].split()[1], clock, f"frozen after resizing to {width}x{height}")
        s.select("gamma-33")
        s.step("Down")
        self.assertIn("beta-22", s.selected_line())

    def test_18_drill_into_a_subagent(self):
        s = self.s
        s.select_line("Write the tests")
        s.press("Enter")
        s.wait_for("Write the tests (in alpha-11)")
        for needle in ("Write unit tests for app.py", "All 12 tests pass", "its steps, newest first",
                       "its own helpers: Lint fixer"):
            self.assertIn(needle, s.text())
        s.press("Escape")
        s.wait_for("Blocked on you")
        s.press("m")
        s.wait_for("select a session row to message it")

    def test_19_stop_a_stray_agent_process(self):
        s = self.s
        s.select_line(f"pid {FX.stray}")
        s.press("Enter")
        s.wait_for("started by launchd")
        s.press("Escape")
        s.wait_for("Blocked on you")
        s.press("x")
        s.wait_for("press x again")
        s.press("x")
        s.wait_for(f"stopped pid {FX.stray}")
        end = time.time() + 5
        while time.time() < end:
            try:
                os.kill(FX.stray, 0)
                time.sleep(0.1)
            except ProcessLookupError:
                break
        self.assertRaises(ProcessLookupError, os.kill, FX.stray, 0)

    def test_20_close_and_fork_keys(self):
        s = self.s
        s.select("beta-22")
        s.press("C")
        s.wait_for("press C again")
        self.assertNotIn("/closecode", FX.pane("beta-22"))
        s.press("C")
        s.wait_for("sent to beta-22")
        time.sleep(0.5)
        self.assertIn("/closecode", FX.pane("beta-22"))
        s.press("F", pause=0.2)
        s.press("F")
        s.wait_for("sent to beta-22")
        time.sleep(0.5)
        self.assertIn("/forkcode", FX.pane("beta-22"))
        s.select("gamma-33")
        s.press("C")
        s.wait_for("blocked on a prompt")

    def test_21_closed_sessions_section(self):
        s = self.s
        s.wait_for("Closed (resumable)")
        self.assertIn("api-client", s.text())
        # Select the closed session row, enter opens detail
        s.select_line("api-client")
        s.press("Enter")
        s.wait_for("Deploy to staging")
        s.press("Escape")
        s.wait_for("Blocked on you")
        # D D hides it
        s.select_line("api-client")
        s.press("D")
        s.wait_for("press D again")
        s.press("D")
        s.wait_for("hidden api-client")

    def test_23_display_name_shown(self):
        """Sessions with titles show the title as the primary name."""
        s = self.s
        # beta-22 has custom title "changelog-drafter"
        self.assertIn("changelog-drafter", s.text())
        # alpha-11 has AI title "Alpha feature work" shown as display name
        self.assertIn("Alpha feature", s.text())

    def test_24_rename_key(self):
        """N N opens the rename input."""
        s = self.s
        s.select_line("changelog-drafter")
        s.press("N")
        s.wait_for("press N again")
        s.press("N")
        s.wait_for("rename  beta-22:")
        s.press("Escape")

    def test_22_exit_refuses_unsaved(self):
        """E E on a session with unsaved work must refuse and suggest S S."""
        s = self.s
        s.select("beta-22")  # beta is in the work dir which has uncommitted files
        s.press("E")
        s.wait_for("unsaved work")

    def test_98_commands_never_get_the_terminal_as_stdin(self):
        """The real `claude agents --json` reads a tty stdin and would swallow the keys meant for atc."""
        with open(os.path.join(FX.stub, "stdin.log")) as fh:
            seen = fh.read().split()
        self.assertTrue(seen)
        self.assertNotIn("True", seen)

    def test_99_the_screen_loop_never_stalls(self):
        """Every pass of the loop (refresh, draw, wait for keys) stays quick, so keys are never ignored."""
        with open(self.s.debug) as fh:
            lines = fh.read().splitlines()
        refreshes = [float(line.split()[2].rstrip("s")) for line in lines if " refresh " in line][1:]  # skip start-up
        passes = [float(line.split()[2].rstrip("s")) for line in lines if " loop " in line][1:]
        self.assertGreater(len(passes), 50)
        self.assertLess(max(refreshes), 0.3, sorted(refreshes)[-5:])
        self.assertLess(max(passes), 0.6, sorted(passes)[-5:])


class Server(unittest.TestCase):
    """A server over (fake) ssh: its rows can be selected, its stray process stopped, its sessions opened."""

    @classmethod
    def setUpClass(cls):
        if LIVE_SKIP:
            raise unittest.SkipTest(LIVE_SKIP)
        cls.log = os.path.join(FX.root, "ssh.log")
        cls.ssh = os.path.join(FX.stub, "fake-ssh")
        with open(cls.ssh, "w") as fh:
            fh.write(f"""#!/bin/sh
while [ "$1" = -o ] || [ "$1" = -t ]; do [ "$1" = -o ] && shift; shift; done
host=$1; shift
echo "ssh $host $*" >> {cls.log}
if [ "$1" = kill ]; then exit 0; fi
"$@" | {sys.executable} -c 'import json,sys; d=json.load(sys.stdin); d["machine"]="fakebox"; d["agents"]=[{{"pid": 4242, "tool": "codex", "etime": "5-00:00:00", "age": 432000, "stale": True, "parent": "openclaw", "tty": "", "command": "node codex exec stuck"}}]; print(json.dumps(d))'
""")
        os.chmod(cls.ssh, 0o755)
        cls.s = Screen(160, 50, "--host", "fakebox", hosts=True, extra_env={"ATC_SSH": cls.ssh}, ready="5 Servers")
        cls.s.press("5")
        cls.s.wait_for("── fakebox")

    @classmethod
    def tearDownClass(cls):
        code, err = cls.s.quit()
        assert code == "0" and not err.strip(), (code, err)

    def test_select_and_stop_a_server_process(self):
        s = self.s
        s.select_line("pid 4242")
        self.assertIn("stale?", s.selected_line())
        s.press("Enter")
        s.wait_for("on fakebox")
        s.press("Escape")
        s.wait_for("── fakebox")
        s.press("x")
        s.press("x")
        s.wait_for("stopped pid 4242 on fakebox")
        with open(self.log) as fh:
            self.assertIn("ssh fakebox kill 4242", fh.read())

    def test_enter_on_a_server_session_opens_ssh_there(self):
        s = self.s
        rows = [i for i, line in enumerate(s.lines()) if line.startswith("──") and "fakebox" in line]
        self.assertTrue(rows)
        s.select_line("pid 4242")  # the server section: its sessions are listed above its processes
        s.step("Up")
        s.press("Enter")
        s.wait_for("ssh -t fakebox")


class Narrow(unittest.TestCase):
    """100 columns: one bottom panel, cycled with b."""

    @classmethod
    def setUpClass(cls):
        if LIVE_SKIP:
            raise unittest.SkipTest(LIVE_SKIP)
        cls.s = Screen(100, 30)

    @classmethod
    def tearDownClass(cls):
        code, err = cls.s.quit()
        assert code == "0" and not err.strip(), (code, err)

    def test_b_cycles_three_panels(self):
        s = self.s
        s.wait_for("── Activity, all sessions")
        s.press("b")
        s.wait_for("── Selected: ")
        s.press("b")
        s.wait_for("── Comms (c opens the full view)")
        s.press("b")
        s.wait_for("── Activity, all sessions")

    def test_page_keys_scroll_activity(self):
        s = self.s
        s.press("PageDown", "PageUp", "J", "K")
        self.assertIn(" atc ", s.text())


class Tiny(unittest.TestCase):
    def test_a_tiny_window_still_runs_and_quits(self):
        if LIVE_SKIP:
            self.skipTest(LIVE_SKIP)
        s = Screen(60, 14)
        s.press("Down", "Tab", "c", "Escape", "h", "Escape", "?", "Escape")
        self.assertIn(" atc ", s.text())
        code, err = s.quit()
        self.assertEqual(code, "0")
        self.assertEqual(err.strip(), "")


def tabs_fixture():
    fx = fixture.AmailFixture()
    fx.write_closed_transcript()
    skill = Path(fx.env()["HOME"], ".claude", "skills", "review", "SKILL.md")
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("# Review\nA fixture skill.\n")
    swarm = fixture.SwarmFixture()
    try:
        status = swarm.status("release", "release-swarm", state="done")
        status["repo"] = fx.work
        path = Path(fx.root, "route", "swarms", "release", "status.json")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(status))
    finally:
        swarm.stop()
    for name, body in (("quotamax", fixture.STUB_QUOTAMAX), ("route", fixture.STUB_ROUTE)):
        path = Path(fx.stub, name)
        path.write_text(body)
        path.chmod(0o755)
    with open(os.path.join(fx.stub, "processes.txt"), "a") as fh:
        fh.write("900 1 ? 02:00 1024 codex exec fixture-worker\n")
    server = {"machine": "server1", "counts": {"working": 1}, "groups": {"working": [
        {"sid": "remote-session", "name": "server-session", "group": "working", "state": "busy",
         "doing": "Building on server", "cwd": "/tmp/project"}]}, "agents": []}
    Path(fx.stub, "server.json").write_text(json.dumps(server))
    ssh = Path(fx.stub, "ssh")
    ssh.write_text("#!/usr/bin/env python3\nimport os, sys\nsys.stdin.buffer.read()\n"
                   "print(open(os.path.join(os.path.dirname(__file__), 'server.json')).read())\n")
    ssh.chmod(0o755)
    return fx


def tabs_env(fx):
    env = fx.env(ATC_AMAIL=os.path.join(fx.stub, "amail"), ATC_LOOM=os.path.join(fx.stub, "loom"),
                 ATC_LOOM_HOSTS="server1", ATC_SSH=os.path.join(fx.stub, "ssh"))
    env.update(ATC_QUOTAMAX=os.path.join(fx.stub, "quotamax"), ATC_ROUTE=os.path.join(fx.stub, "route"))
    return env


class TabLogic(unittest.TestCase):
    """The same fixture and rendering without a tty, including races and missing data."""

    def setUp(self):
        self.fx = tabs_fixture()
        self.addCleanup(self.fx.stop)
        self.env = tabs_env(self.fx)
        patch = mock.patch.dict(os.environ, self.env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.m = load_atc(self.env)
        m = self.m
        feed = m.Feed([self.env["ATC_CLAUDE"]], background=False)
        feed.fetch()
        mail, loom = m.AMail(background=False), m.Loom(background=False)
        mail.fetch()
        loom.fetch()
        self.fleet = m.Fleet(feed=feed, amail=mail, loom=loom, closed=m.ClosedSessions(background=False))
        self.fleet.refresh(time.time())
        self.quota = m.Quota([self.env["ATC_QUOTAMAX"]], background=False)
        self.quota.fetch()
        remote = m.Remote("server1", background=False)
        remote.data = json.loads(Path(self.fx.stub, "server.json").read_text())
        remote.ok_at, remote.latency = time.time(), 0
        self.model = self.fleet.model(time.time(), self.quota, [remote])
        self.app = m.App(self.fleet, self.quota, m.Terminals(True), [self.env["ATC_ROUTE"]],
                         [self.env["ATC_CLAUDE"]], True)
        self.app.switch_tab("sessions", self.model)

    def text(self, frame):
        return "\n".join(self.m.plain(line) for line in frame)

    def palette(self, query):
        self.app.handle(":", self.model)
        for char in query:
            self.app.handle(char, self.model)
        return self.app.palette_actions(self.model)

    def test_tab_visibility_numbers_and_selection_survive_refresh(self):
        app, model, m = self.app, self.model, self.m
        beta = self.fx.sid("beta-22")
        app.ui["selected"] = beta
        app.handle("2", model)
        self.assertEqual(app.ui["tab"], "needs")
        app.handle(m.curses.KEY_RIGHT, model)
        self.assertEqual((app.ui["tab"], app.ui["view"]), ("agents", "tree"))
        app.handle("c", model)
        self.assertEqual(app.ui["view"], "comms")
        app.handle("4", model)
        app.handle(m.curses.KEY_LEFT, model)
        self.assertEqual(app.ui["view"], "comms")
        app.handle("1", model)
        self.assertEqual(app.ui["selected"], beta)
        empty = dict(model, needs=[], jobs={}, hosts=[], quota=None, groups={k: [] for k in model["groups"]},
                     agents=[], swarms=[], comms=[], closed=[])
        self.assertEqual([n for n, _, _ in m.visible_tabs(empty, {})], ["1"])
        self.assertEqual([n for n, _, _ in m.visible_tabs(empty, app.ui)], list("123456"))
        app.handle("2", empty)
        self.assertIn("Nothing needs you", self.text([line for line, _ in m.tab_rows(empty, app.ui, 100)]))

    def test_tabs_render_cached_data_and_do_not_mutate_once_output(self):
        m, model, app = self.m, self.model, self.app
        once = self.text(m.render(self.fleet, model, app.ui, 180)[0])
        before = self.fx.calls()
        expected = {"sessions": "Closed (resumable)", "needs": "a-mail 2 approvals", "jobs": "app dashboard",
                    "servers": "Building on server", "quota": "quotamax advice"}
        with mock.patch.object(m, "run", side_effect=AssertionError("render must not run commands")):
            for tab, needle in expected.items():
                app.switch_tab(tab, model)
                frame = self.text(m.render(self.fleet, model, app.ui, 180, 90)[0])
                self.assertIn(needle, frame)
                if tab == "sessions":
                    self.assertNotIn("Jobs & apps", frame)
                    self.assertNotIn("Building on server", frame)
                elif tab == "quota":
                    for text in ("Codex (test)", "weekly 19%", "DeepSeek", "balance $9.50", "parallelism: 4", "auto"):
                        self.assertIn(text, frame)
            self.assertEqual(self.text(m.render(self.fleet, model, app.ui, 180)[0]), once)
        self.assertEqual(self.fx.calls(), before)
        self.assertNotIn("1 Sessions", once)
        self.assertIn("Jobs & apps", once)
        self.assertIn("Building on server", once)

    def test_footer_capabilities_per_row_and_views(self):
        m, app, model = self.m, self.app, self.model
        helper = next(key for _, key in m.tab_rows(model, app.ui, 180) if key and "::" in key and not key.startswith("@"))
        cases = [(self.fx.sid("beta-22"), "m msg", "R R restore"),
                 (self.fx.sid("gamma-33"), "enter jump", "m msg"),
                 (helper, "enter detail", "x x stop"), ("@local::p::900", "x x stop", "m msg"),
                 ("closed::" + self.fx.closed_sid, "R R restore", "E exit"),
                 ("@server1::s::remote-session", "enter ssh", "m msg"),
                 (m.job_key("local", "crashed"), "x x run now", "x x stop")]
        need = next(n for n in model["needs"] if n["kind"] == "amail_request")
        cases.append((m.need_key(need), "enter detail", "y y approve"))
        cases.append(("swarm::release", "enter detail", "x x stop"))
        for key, present, absent in cases:
            with self.subTest(key=key):
                app.ui["selected"] = key
                footer = m.plain(app.footer(time.time(), model))
                self.assertIn(present, footer)
                self.assertNotIn(absent, footer)
                self.assertIn(": actions", footer)
        app.ui.update(view="tree", tab="agents")
        footer = m.plain(app.footer(time.time(), model))
        self.assertIn("s all/selected", footer)
        self.assertNotIn("m msg", footer)

    def test_palette_filters_selects_and_reuses_confirmation(self):
        app, m = self.app, self.m
        actions = self.palette("raise swarm cap")
        self.assertEqual(len(actions), 1)
        app.handle("\n", self.model)
        self.assertIn("dry run:", app.flash[0])
        self.assertNotIn("override abundant", self.fx.calls())
        actions = self.palette("jump beta-22")
        self.assertEqual(len(actions), 1)
        with mock.patch.object(m, "jump_to", return_value="dry run: focus") as jump:
            app.handle("\n", self.model)
            self.assertEqual(jump.call_args.args[-1], True)
        self.assertEqual(app.ui["selected"], self.fx.sid("beta-22"))
        self.assertIn("dry run", app.flash[0])
        self.palette("save and close session")
        with mock.patch.object(m, "deliver", return_value=(True, "dry run")) as deliver:
            app.handle("\n", self.model)
            deliver.assert_not_called()
            self.assertIn("press S again", app.flash[0])
            app.handle("S", self.model)
            self.assertEqual(deliver.call_args.args[-2:], ("/closecode", True))
        self.palette("no-action-matches-this")
        app.handle("\n", self.model)
        self.assertEqual(app.mode, "palette")
        app.handle("\x1b", self.model)
        self.assertEqual(app.mode, "normal")

    def test_run_command_preview_cancel_dry_run_and_state_change(self):
        app, model, m = self.app, self.model, self.m
        app.ui["selected"] = self.fx.sid("beta-22")
        self.palette("run /status")
        app.handle("\n", model)
        self.assertEqual(app.mode, "command_confirm")
        self.assertIn("Type /status into beta-22?", m.plain(app.footer(time.time(), model)))
        with mock.patch.object(m, "deliver", return_value=(True, "dry run")) as deliver:
            app.handle("\x1b", model)
            deliver.assert_not_called()
            self.palette("run /status")
            app.handle("\n", model)
            app.handle("\n", model)
            self.assertEqual(deliver.call_args.args[-2:], ("/status", True))
            self.palette("run /status")
            app.handle("\n", model)
            v = app.selected_view(model)
            v["group"] = "blocked"
            app.handle("\n", model)
            self.assertEqual(deliver.call_count, 1)
            self.assertIn("no longer idle", app.flash[0])

    def test_palette_approval_and_remote_needs_cannot_decide_locally(self):
        app, m = self.app, self.m
        row = self.fleet.approvals.create("atc_message", {}, "Message beta-22", "caller", "fixture")
        self.model = self.fleet.model(time.time(), self.quota)
        self.palette("approve " + row["id"])
        app.handle("\n", self.model)
        self.assertEqual(app.ui["tab"], "needs")
        self.assertIn("press y again", app.flash[0])
        app.handle("y", self.model)
        self.assertIn("dry run: approve", app.flash[0])
        self.assertEqual(self.fleet.approvals.get(row["id"])["state"], "pending")
        remote = dict(next(n for n in self.model["needs"] if n["kind"] == "approval"), host="server1")
        self.model["needs"] = [remote]
        app.ui["selected"] = m.need_key(remote)
        app.flash = ("", 0)
        self.assertNotIn("y y approve", m.plain(app.footer(time.time(), self.model)))
        self.assertEqual(m.resolve(self.fleet, self.model, app.ui["selected"])[0], "need")

    def test_command_exit_keeps_unsaved_guard_and_palette_works_from_views(self):
        app, model, m = self.app, self.model, self.m
        app.ui["selected"] = self.fx.sid("beta-22")
        app.selected_view(model)["repo"] = {"dirty": 1, "unpushed": 0}
        self.palette("run /exit")
        app.handle("\n", model)
        with mock.patch.object(m, "deliver") as deliver:
            app.handle("\n", model)
            deliver.assert_not_called()
        self.assertIn("unsaved work", app.flash[0])
        app.handle("h", model)
        self.palette("message selected session")
        app.handle("\n", model)
        self.assertEqual(app.mode, "message")
        self.assertEqual(app.targets[0]["sid"], self.fx.sid("beta-22"))
        self.assertIsNone(app.ui["view"])

    def test_slow_route_can_be_cancelled_without_blocking_keys(self):
        import threading
        app, model, m = self.app, self.model, self.m
        gate, entered = threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        app.in_tui = True

        def plan(*args):
            entered.set()
            gate.wait(3)
            return {"chosen": "codex"}, None

        with mock.patch.object(m, "route_plan", side_effect=plan):
            app.handle("n", model)
            app.buffer = "test the queue"
            started = time.monotonic()
            app.handle("\n", model)
            self.assertLess(time.monotonic() - started, 0.3)
            self.assertTrue(entered.wait(1))
            app.handle("\x1b", model)
            app.handle("2", model)
            self.assertEqual(app.ui["tab"], "needs")
            gate.set()
        self.assertEqual(app.mode, "normal")

    def test_broadcast_replaces_stale_result_and_keeps_project_targets(self):
        app, model, m = self.app, self.model, self.m
        views = {v["name"]: v for v in m.ordered_sessions(model, True)}
        views["beta-22"]["cwd"] = self.fx.work
        views["delta-44"]["cwd"] = os.path.join(self.fx.root, "parked")
        app.submit_message("blocked message", [views["gamma-33"]])
        self.assertIn("skipped gamma-33", app.flash[0])
        app.ui["selected"] = views["beta-22"]["sid"]
        app.handle("M", model)
        self.assertEqual({v["name"] for v in app.targets}, {"alpha-11", "beta-22", "gamma-33"})
        app.buffer = "sync up"

        with mock.patch.object(app, "perform") as perform:
            app.handle("\n", model)

        self.assertEqual(app.flash[0], "sending to 3 sessions…")
        app.switch_tab("needs", model)
        app.targets = [views["gamma-33"]]
        work, text, targets = perform.call_args.args
        with mock.patch.object(m, "deliver", side_effect=lambda _f, _t, v, *_args:
                               (False, "blocked") if v["group"] == "blocked" else (True, "dry run")) as deliver:
            work(text, targets)
        self.assertEqual({call.args[2]["name"] for call in deliver.call_args_list},
                         {"alpha-11", "beta-22", "gamma-33"})
        self.assertIn("beta-22, alpha-11", app.flash[0])
        self.assertIn("skipped gamma-33", app.flash[0])

    def test_jobs_apps_after_the_last_job_are_selectable_and_read_only(self):
        app, model, m = self.app, self.model, self.m
        model["jobs"] = {"local": model["jobs"]["local"]}
        entry = model["jobs"]["local"]
        entry["apps"] = [dict(entry["apps"][0], name=f"app-{i:02d}") for i in range(20)]
        app.switch_tab("jobs", model)
        last = m.app_key("local", "app-19")
        self.assertIn(last, app.selectable)
        for _ in app.selectable:
            app.handle(m.curses.KEY_DOWN, model)
        frame = m.render(self.fleet, model, app.ui, 80, 14)[0]
        self.assertEqual(app.ui["selected"], last)
        self.assertIn("app-19", self.text(frame))
        footer = m.plain(app.footer(time.time(), model))
        self.assertIn("enter detail", footer)
        self.assertNotIn("run now", footer)
        with mock.patch.object(m, "run", side_effect=AssertionError("app details are cached")):
            app.handle("\n", model)
            frame = m.render_view(self.fleet, model, app.ui, 80, 20)
            self.assertIn("URL: https://example.com", self.text(frame))
            app.handle("\x1b", model)
            app.handle("x", model)
            app.handle("x", model)
        self.assertNotIn("jobs run", self.fx.calls())


@unittest.skipUnless(TMUX, "tmux is needed for the screen tests")
class Tabs(unittest.TestCase):
    """Real curses screens with fixed process data, also runnable when ps is denied."""

    @classmethod
    def setUpClass(cls):
        cls.fx = tabs_fixture()
        cls.addClassCleanup(cls.fx.stop)
        cls.target = f"atc-tabs-target-{os.getpid()}"
        try:
            result = subprocess.run([TMUX, "new-session", "-d", "-s", cls.target, "sleep 3600"],
                                    capture_output=True, text=True)
        except PermissionError as exc:
            raise unittest.SkipTest(f"screen fixture needs tmux; sandbox denied it: {exc}") from exc
        if result.returncode:
            if any(reason in result.stderr.lower() for reason in ("operation not permitted", "permission denied")):
                raise unittest.SkipTest("screen fixture needs the tmux socket; sandbox denied it: " + result.stderr.strip())
            raise AssertionError(result.stderr)
        cls.addClassCleanup(subprocess.run, [TMUX, "kill-session", "-t", cls.target], capture_output=True)
        tty = subprocess.run([TMUX, "display-message", "-p", "-t", cls.target, "#{pane_tty}"],
                             check=True, capture_output=True, text=True).stdout.strip()
        processes = Path(cls.fx.stub, "processes.txt")
        processes.write_text(processes.read_text().replace(" ? ", " " + tty.removeprefix("/dev/") + " "))
        cls.env = tabs_env(cls.fx)
        with mock.patch.dict(os.environ, cls.env, clear=True):
            cls.m = load_atc(cls.env)
            cls.approvals = cls.m.Approvals()
            cls.approval = cls.approvals.create("atc_message", {}, "Message beta-22", "caller", "fixture")
        cls.s = Screen(160, 45, "--dry-run", "--host", "server1", hosts=True, fx=cls.fx,
                       extra_env=cls.env, ready="6 Quota")

    @classmethod
    def tearDownClass(cls):
        code, err = cls.s.quit()
        assert (code, err.strip()) == ("0", ""), (code, err)

    def setUp(self):
        self.s.press("Escape", "1")

    def test_01_tab_numbers_arrows_content_and_selection(self):
        s = self.s
        self.assertEqual(s.lines()[0].strip(), "1 Sessions · 2 Needs · 3 Agents · 4 Jobs · 5 Servers · 6 Quota · 7 Tools")
        s.select("beta-22")
        s.press("2")
        s.wait_for("── Needs you")
        self.assertIn("a-mail 2 approvals", s.text())
        self.assertNotIn("── Working", s.text())
        s.press("Right")
        s.wait_for("Hierarchy: all sessions")
        self.assertIn("workflow release-train", s.text())
        s.press("c")
        s.wait_for("Comms: all sessions")
        s.press("4")
        s.wait_for("── Jobs & apps")
        self.assertIn("app dashboard", s.text())
        self.assertNotIn("── Working", s.text())
        s.press("Right")
        s.wait_for("Building on server")
        s.press("6")
        s.wait_for("── quotamax advice")
        for needle in ("Codex (test)", "DeepSeek", "parallelism: 4", "Swarm cap", "auto"):
            self.assertIn(needle, s.text())
        s.press("Right")
        s.wait_for("── Tools")
        self.assertIn("Skills", s.text())
        s.press("Right")
        s.wait_for("── Blocked on you")
        self.assertIn("beta-22", s.selected_line())
        s.press("Left")
        s.wait_for("── Tools")
        s.press("Left")
        s.wait_for("── quotamax advice")

    def test_02_palette_filter_arrows_escape_and_dry_run(self):
        s = self.s
        s.press(":")
        s.wait_for("Command palette")
        s.type("swarm cap")
        s.wait_for("raise swarm cap")
        self.assertNotIn("message selected session", s.text())
        s.press("Down")
        self.assertIn("lower swarm cap", s.selected_line())
        s.press("Up", "Enter")
        s.wait_for("dry run:")
        self.assertNotIn("quotamax override abundant", self.fx.calls())
        s.press(":")
        s.type("jump beta-22")
        s.press("Enter")
        s.wait_for("dry run: would focus")
        self.assertIn("beta-22", s.selected_line())
        s.press(":")
        s.type("no-such-action")
        s.wait_for("No matching actions")
        s.press("Enter", "Escape")
        s.wait_for("Command palette", absent=True)

    def test_03_palette_command_confirmation(self):
        s = self.s
        s.select("beta-22")
        s.press(":")
        s.type("run /status")
        s.press("Enter")
        s.wait_for("Type /status into beta-22?")
        s.press("Enter")
        s.wait_for("dry run: message to beta-22")

    def test_04_contextual_footer(self):
        s = self.s
        cases = [("beta-22", "m msg", "R R restore"), ("Write the tests", "enter detail", "m msg"),
                 ("release-swarm", "enter detail", "x x stop"),
                 ("pid 900", "x x stop", "m msg"), ("api-client", "R R restore", "m msg")]
        for target, present, absent in cases:
            s.select(target)
            self.assertIn(present, s.footer())
            self.assertNotIn(absent, s.footer())
        s.press("5")
        s.wait_for("enter ssh")
        self.assertNotIn("m msg", s.footer())
        s.press("4")
        s.wait_for("x x run now")
        s.press("2")
        s.select("a-mail 3 requests")
        self.assertIn("enter detail", s.footer())
        self.assertNotIn("y y approve", s.footer())

    def test_05_approval_palette_uses_existing_keys(self):
        s = self.s
        s.press(":")
        s.type("approve " + self.approval["id"])
        s.press("Enter")
        s.wait_for("press y again")
        s.press("y")
        s.wait_for("dry run: approve " + self.approval["id"])
        self.assertEqual(self.approvals.get(self.approval["id"])["state"], "pending")

    def test_06_b_panel_and_legacy_tab_alias(self):
        s = self.s
        s.select("beta-22")
        s.wait_for("── Activity, all sessions")
        s.press("b")
        s.wait_for("── Selected: beta-22")
        s.press("Tab")
        s.wait_for("── Activity, all sessions")

    def test_07_small_screen_help_scroll_and_tabs(self):
        s = self.s
        try:
            subprocess.run([TMUX, "resize-window", "-t", s.name, "-x", "60", "-y", "14"], check=True)
            s.press("b")
            s.wait_for("── Selected:")
            s.press("b")
            s.wait_for("── Comms (c opens")
            s.press("b")
            s.wait_for("── Activity, all sessions")
            s.press("6")
            s.wait_for("6 Quota")
            self.assertIn("6 Quota", s.lines()[0])
            s.press("?")
            s.wait_for("── Keys")
            s.press(*(["PageDown"] * 8))
            s.wait_for("quit (esc closes")
            s.press("Escape")
        finally:
            subprocess.run([TMUX, "resize-window", "-t", s.name, "-x", "160", "-y", "45"], check=True)

    def test_08_tools_skill_picker_and_dry_run(self):
        s = self.s
        s.press("7")
        s.wait_for("── Tools")
        s.wait_for("review · 0 uses")
        s.select("review")
        s.press("r")
        s.wait_for("Run /review in")
        s.press("Enter")
        s.wait_for("type /review into")
        s.press("Escape")
        s.wait_for("enter SKILL.md")
        s.press("r", "Enter", "Enter")
        s.wait_for("dry run: would type")
        s.press("1")

    def test_10_slow_action_does_not_freeze_the_screen(self):
        s = self.s
        route = Path(self.fx.stub, "route")
        original = route.read_text()
        route.write_text(original.replace("import json, os, sys", "import json, os, sys, time\ntime.sleep(2)"))
        try:
            s.press("n")
            s.type("slow plan")
            s.press("Enter", pause=0.05)
            s.wait_for("Planning with route")
            s.press("Escape", "2", pause=0.05)
            s.wait_for("── Needs you", timeout=1)
            time.sleep(2.2)
            self.assertIn("── Needs you", s.text())
            self.assertNotIn("dispatch in a new tab", s.footer())
        finally:
            route.write_text(original)

    def test_09_jobs_app_rows_scroll_past_the_last_job(self):
        s = self.s
        try:
            subprocess.run([TMUX, "resize-window", "-t", s.name, "-x", "60", "-y", "14"], check=True)
            s.press("4")
            s.select("app dashboard")
            self.assertIn("app dashboard", s.selected_line())
            self.assertIn("enter detail", s.footer())
            self.assertNotIn("run now", s.footer())
            s.press("Enter")
            s.wait_for("URL: https://example.com")
            s.press("Escape")
        finally:
            subprocess.run([TMUX, "resize-window", "-t", s.name, "-x", "160", "-y", "45"], check=True)

    def test_99_screen_loop_stays_quick(self):
        lines = Path(self.s.debug).read_text().splitlines()
        passes = [float(line.split()[2].rstrip("s")) for line in lines if " loop " in line][1:]
        refreshes = [float(line.split()[2].rstrip("s")) for line in lines if " refresh " in line][1:]
        self.assertGreater(len(passes), 50)
        self.assertLess(max(passes), 0.6, sorted(passes)[-5:])
        self.assertLess(max(refreshes), 0.3, sorted(refreshes)[-5:])


if __name__ == "__main__":
    unittest.main()
