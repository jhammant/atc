"""Screen tests: atc runs in tmux against the fake Claude home, keys are pressed, and the screen is read back
exactly as drawn. Steering is checked in the dummy sessions' own terminals.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import os
import shlex
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixture  # noqa: E402

FX = None
TMUX = fixture.TMUX


def setUpModule():
    global FX
    if not TMUX:
        raise unittest.SkipTest("tmux is needed for the screen tests")
    FX = fixture.Fixture().start()


def tearDownModule():
    if FX:
        FX.stop()


class Screen:
    """atc running in a tmux window of its own, with keys in and the screen out."""

    def __init__(self, width, height, *args, hosts=False, extra_env=None, ready="Blocked on you"):
        self.name = f"atc-ui-{os.getpid()}-{width}x{height}"
        self.err = os.path.join(FX.root, f"{self.name}.err")
        self.code = os.path.join(FX.root, f"{self.name}.code")
        self.debug = os.path.join(FX.root, f"{self.name}.debug")
        env = FX.env(ATC_DEBUG_LOG=self.debug, **(extra_env or {}))
        exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items() if k.startswith(("ATC_", "CLAUDE_", "TERM")))
        hosts_arg = "" if hosts else "--no-hosts"
        command = (f"cd {shlex.quote(FX.work)} && env {exports} HOME={shlex.quote(env['HOME'])} "
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

    def test_03_tab_switches_the_left_panel(self):
        s = self.s
        s.select("beta-22")
        s.wait_for("── Activity, all sessions")
        s.press("Tab")
        s.wait_for("── Selected: beta-22")
        self.assertIn("said   Done. Want me to ship it?", s.text())
        s.press("Tab")
        s.wait_for("── Activity, all sessions")  # two panels here: comms is already on the right

    def test_04_comms_view(self):
        s = self.s
        s.press("c")
        s.wait_for("Comms: all sessions")
        self.assertIn("alpha-11 → beta-22", s.text())
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
                       "talks with beta-22 ×1"):
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
            clock = s.lines()[0].split()[1]
            time.sleep(2.2)  # still alive: the clock in the header keeps moving after a resize
            self.assertNotEqual(s.lines()[0].split()[1], clock, f"frozen after resizing to {width}x{height}")
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
        cls.s = Screen(160, 50, "--host", "fakebox", hosts=True, extra_env={"ATC_SSH": cls.ssh}, ready="── fakebox")

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
    """100 columns: one bottom panel, cycled with tab."""

    @classmethod
    def setUpClass(cls):
        cls.s = Screen(100, 30)

    @classmethod
    def tearDownClass(cls):
        code, err = cls.s.quit()
        assert code == "0" and not err.strip(), (code, err)

    def test_tab_cycles_three_panels(self):
        s = self.s
        s.wait_for("── Activity, all sessions")
        s.press("Tab")
        s.wait_for("── Selected: ")
        s.press("Tab")
        s.wait_for("── Comms (c opens the full view)")
        s.press("Tab")
        s.wait_for("── Activity, all sessions")

    def test_page_keys_scroll_activity(self):
        s = self.s
        s.press("PageDown", "PageUp", "J", "K")
        self.assertIn(" atc ", s.text())


class Tiny(unittest.TestCase):
    def test_a_tiny_window_still_runs_and_quits(self):
        s = Screen(60, 14)
        s.press("Down", "Tab", "c", "Escape", "h", "Escape", "?", "Escape")
        self.assertIn(" atc ", s.text())
        code, err = s.quit()
        self.assertEqual(code, "0")
        self.assertEqual(err.strip(), "")


if __name__ == "__main__":
    unittest.main()
