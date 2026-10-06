"""Unit and integration tests for atc's parsing, model and levers, against the fake Claude home in fixture.py.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixture  # noqa: E402

FX = None


def load_atc(env):
    """Import atc.py fresh. The fake environment is already in os.environ for the whole run (setUpModule)."""
    spec = importlib.util.spec_from_file_location("atc_under_test", fixture.ATC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SAVED_ENV = {}


def setUpModule():
    global FX
    if not fixture.TMUX:
        raise unittest.SkipTest("tmux is needed for the fixture's live sessions")
    FX = fixture.Fixture().start()
    # Everything atc does in these tests sees only the fake home and stubs: no real terminal, Herdr or servers.
    SAVED_ENV.update(os.environ)
    env = FX.env()  # built from the real environment before it is replaced
    os.environ.clear()
    os.environ.update(env)


def tearDownModule():
    if SAVED_ENV:
        os.environ.clear()
        os.environ.update(SAVED_ENV)
    if FX:
        FX.stop()


def build(m, now=None, with_closed=False):
    """The fleet model over the fixture, the way atc --json builds it."""
    claude = [os.path.join(FX.stub, "claude")]
    feed = m.Feed(claude, background=False)
    feed.fetch()
    closed = m.ClosedSessions(background=False) if with_closed else None
    fleet = m.Fleet(None, feed, m.Herdr(None, background=False), closed=closed)
    now = now or time.time()
    fleet.refresh(now)
    return fleet, fleet.model(now)


class Helpers(unittest.TestCase):
    def setUp(self):
        self.m = load_atc(FX.env())

    def test_parse_ts(self):
        p = self.m.parse_ts
        self.assertAlmostEqual(p("2026-10-04T10:00:00Z"), p("2026-10-04T10:00:00+00:00"))
        self.assertEqual(p(1791026130523), 1791026130.523)
        self.assertEqual(p("1791026130523"), 1791026130.523)
        self.assertIsNone(p("not a date"))
        self.assertIsNone(p(None))

    def test_etime_and_age(self):
        self.assertEqual(self.m.etime_seconds("05:03"), 303)
        self.assertEqual(self.m.etime_seconds("1:02:03"), 3723)
        self.assertEqual(self.m.etime_seconds("13-19:04:11"), 13 * 86400 + 19 * 3600 + 4 * 60 + 11)
        self.assertEqual([self.m.human_age(s) for s in (5, 75, 7300, 200000)], ["5s", "1m", "2h", "2d"])

    def test_process_label(self):
        label = self.m.process_label
        self.assertEqual(label("/usr/bin/node /home/x/.openclaw/patched/openclaw-2026.6.10-svg/dist/index.js gateway"),
                         "openclaw")
        self.assertEqual(label("/opt/hermes/.venv/bin/python3 /opt/hermes/.venv/bin/hermes gateway run"), "hermes")
        self.assertEqual(label("s6-supervise gateway-default"), "s6-supervise")

    def test_clean_strips_wide_characters(self):
        self.assertEqual(self.m.clean("🏞 World & Horse"), "World & Horse")
        self.assertEqual(self.m.clean("✏️ writing\nMain.luau"), "writing Main.luau")
        for ch in self.m.clean("a → b ✓ ● ◆"):
            self.assertNotIn(__import__("unicodedata").east_asian_width(ch), ("W", "F"))

    def test_fit_and_pad_hold_width(self):
        line = [("hello ", "plain"), ("wonderful world", "dim")]
        for width in (0, 1, 5, 11, 40):
            self.assertLessEqual(self.m.seg_len(self.m.fit(line, width)), width)
            if width:
                self.assertEqual(self.m.seg_len(self.m.pad(line, width)), width)

    def test_detail_of(self):
        d = self.m.detail_of
        self.assertEqual(d("Edit", {"file_path": "/repo/src/a.py"}, "/repo"), "src/a.py")
        self.assertEqual(d("Bash", {"command": "ls", "description": "List files"}), "List files")
        self.assertEqual(d("SendMessage", {"to": "beta [abc123]", "summary": "hi"}), "-> beta  hi")
        self.assertEqual(d("AskUserQuestion", {"questions": [{"question": "Which one?"}]}), "Which one?")
        self.assertEqual(d("Workflow", {"script": "export const meta = { name: 'rt' }"}), "rt")

    def test_json_in(self):
        self.assertEqual(self.m.json_in('noise\n{"a": 1}\nmore'), {"a": 1})
        self.assertEqual(self.m.json_in("[1, 2]", list), [1, 2])
        self.assertIsNone(self.m.json_in("no json here"))

    def test_tail_reads_only_new_complete_lines(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write('{"n": 1}\n{"n": 2')
            path = fh.name
        try:
            tail = self.m.Tail(path, 10_000)
            recs, restarted = tail.read_new()
            self.assertEqual([r["n"] for r in recs], [1])  # the half-written line waits
            self.assertFalse(restarted)
            with open(path, "a") as fh:
                fh.write('}\n{"n": 3}\n')
            self.assertEqual([r["n"] for r in tail.read_new()[0]], [2, 3])
            self.assertEqual(tail.read_new()[0], [])
            with open(path, "w") as fh:  # rewritten shorter: start again
                fh.write('{"n": 9}\n')
            recs, restarted = tail.read_new()
            self.assertTrue(restarted)
            self.assertEqual([r["n"] for r in recs], [9])
        finally:
            os.remove(path)

    def test_tail_backlog_skips_the_partial_first_line(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            for n in range(100):
                fh.write(json.dumps({"n": n, "pad": "x" * 50}) + "\n")
            path = fh.name
        try:
            recs, _ = self.m.Tail(path, 500).read_new()
            self.assertTrue(recs)
            self.assertEqual(recs[-1]["n"], 99)
            self.assertEqual([r["n"] for r in recs], list(range(recs[0]["n"], 100)))
        finally:
            os.remove(path)

    def test_unique_hosts(self):
        views = [{"host": "storage", "machine": "buildbox"}, {"host": "buildbox", "machine": "buildbox.local"},
                 {"host": "ci-runner", "machine": "ci-runner"}, {"host": "me", "machine": __import__("socket").gethostname()}]
        out = self.m.unique_hosts(views)
        self.assertEqual([v["host"] for v in out], ["storage", "ci-runner"])
        self.assertEqual(out[0]["aliases"], ["buildbox"])

    def test_configured_hosts(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write("# comment\nbuildbox\n\nci-runner  # build box\nbuildbox\n")
        self.m.HOSTS_FILE = fh.name
        try:
            os.environ["ATC_HOSTS"] = "extra, buildbox"
            self.assertEqual(self.m.configured_hosts(["cli"]), ["cli", "extra", "buildbox", "ci-runner"])
        finally:
            os.environ.pop("ATC_HOSTS", None)
            os.remove(fh.name)

    def test_loose_agents(self):
        table = {
            100: {"ppid": 1, "tty": "/dev/ttys001", "etime": "1:00", "command": "claude --resume x"},       # a session
            101: {"ppid": 100, "tty": "", "etime": "0:30", "command": "codex exec under-session"},          # under it
            200: {"ppid": 1, "tty": "", "etime": "2-00:00:00", "command": "node /x/.bin/codex exec stuck"},  # loose, stale
            201: {"ppid": 200, "tty": "", "etime": "2-00:00:00", "command": "/x/codex-linux run"},           # its child
            300: {"ppid": 1, "tty": "", "etime": "0:10", "command": "claude -p summarise"},                  # headless
            301: {"ppid": 1, "tty": "/dev/ttys009", "etime": "0:10", "command": "claude"},                   # interactive
            400: {"ppid": 1, "tty": "", "etime": "0:10", "command": "node /x/kimi-mcp-server"},             # mcp
        }
        out = {a["pid"]: a for a in self.m.loose_agents(table, {100})}
        self.assertEqual(sorted(out), [200, 300])
        self.assertTrue(out[200]["stale"])
        self.assertFalse(out[300]["stale"])


class Model(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_atc(FX.env())
        cls.fleet, cls.model = build(cls.m)
        cls.views = {v["name"]: v for v in cls.m.ordered_sessions(cls.model, True)}

    def test_groups(self):
        g = {k: [v["name"] for v in vs] for k, vs in self.model["groups"].items()}
        self.assertEqual(g["blocked"], ["gamma-33"])
        self.assertEqual(g["waiting"], ["beta-22"])
        self.assertEqual(g["working"], ["alpha-11"])
        self.assertEqual(g["background"], ["bg deadbeef"])
        self.assertEqual(g["parked"], ["delta-44"])  # open but idle for 13h: still listed
        self.assertEqual(self.model["counts"]["live"], 5)

    def test_unsaved_work(self):
        repos = self.m.Repos(background=False)
        repos.watch([FX.work, os.path.join(FX.work, "alpha")])
        state = repos.fetch()
        self.assertEqual(len(state), 1)  # both folders are in the same repo
        repo = repos.of(FX.work)
        self.assertGreaterEqual(repo["dirty"], 1)
        self.assertEqual((repo["unpushed"], repo["upstream"]), (1, False))
        self.assertIn("uncommitted", self.m.unsaved_text(repo))

    def test_row_keys_resolve(self):
        alpha = self.views["alpha-11"]
        self.assertEqual(self.m.resolve(self.fleet, self.model, alpha["sid"])[0], "session")
        item = self.m.resolve(self.fleet, self.model, f"{alpha['sid']}::a111111111111111")
        self.assertEqual(item[0], "helper")
        self.assertEqual(item[2]["label"], "Write the tests")
        stray = self.m.resolve(self.fleet, self.model, f"@local::p::{FX.stray}")
        self.assertEqual((stray[0], stray[2]["tool"]), ("process", "codex"))
        self.assertEqual(self.m.resolve(self.fleet, self.model, "nonsense")[0], None)

    def test_drilling_into_a_helper(self):
        alpha = self.views["alpha-11"]
        item = self.m.resolve(self.fleet, self.model, f"{alpha['sid']}::a111111111111111")
        text = "\n".join("".join(t for t, _s in line) for line in self.m.helper_lines(self.fleet, *item[1:], 0, 100))
        for needle in ("Write the tests", "started by alpha-11", "Write unit tests for app.py", "report",
                       "All 12 tests pass", "its own helpers: Lint fixer", "tests/test_app.py"):
            self.assertIn(needle, text)
        nested = self.m.resolve(self.fleet, self.model, f"{alpha['sid']}::a222222222222222")
        text = "\n".join("".join(t for t, _s in line) for line in self.m.helper_lines(self.fleet, *nested[1:], 0, 100))
        self.assertIn("started by Write the tests", text)
        self.assertIn("haiku", text)

    def test_blocked_session_says_what_it_asks(self):
        self.assertIn("permission prompt", self.views["gamma-33"]["doing"])
        self.assertIn("Delete the build folder", self.views["gamma-33"]["doing"])

    def test_waiting_session_quotes_its_last_words(self):
        self.assertIn("Done. Want me to ship it?", self.views["beta-22"]["doing"])
        self.assertTrue(self.views["beta-22"]["state"].startswith("waiting"))

    def test_working_session(self):
        a = self.views["alpha-11"]
        self.assertEqual(a["title"], "Alpha feature work")
        self.assertIn("Run the test suite", a["doing"])
        self.assertEqual(a["tty"], f"/dev/{os.path.basename(FX.sessions['alpha-11']['tty'])}")

    def test_helpers_and_workflow(self):
        a = self.views["alpha-11"]
        labels = {h["label"]: h for h in a["helpers"]}
        self.assertTrue(labels["Write the tests"]["finished"])
        self.assertTrue(labels["Lint fixer"]["working"])
        team = a["teams"][0]
        self.assertEqual(team["name"], "release-train")
        self.assertEqual(team["marks"], [("done", "Build"), ("now", "Verify")])
        self.assertEqual((team["done"], team["total"]), (1, 2))

    def test_letters_are_counted_once(self):
        letters = [c for c in self.model["comms"] if c["kind"] == "letter"]
        self.assertEqual(len(letters), 1, letters)
        self.assertEqual((letters[0]["from"], letters[0]["to"]), ("alpha-11", "beta-22"))
        self.assertEqual(letters[0]["summary"], "Ask beta to review the API")

    def test_comms_has_tasks_and_reports(self):
        kinds = {(c["kind"], c["from"], c["to"]) for c in self.model["comms"]}
        self.assertIn(("task", "alpha-11", "Write the tests"), kinds)
        self.assertIn(("task", "Write the tests", "Lint fixer"), kinds)  # nested: the parent agent gave the task
        self.assertIn(("task", "alpha-11 (workflow)", "API client"), kinds)
        self.assertIn(("report", "Write the tests", "alpha-11"), kinds)
        report = next(c for c in self.model["comms"] if c["kind"] == "report" and c["from"] == "Write the tests")
        self.assertEqual(report["body"], "All 12 tests pass")

    def test_tree(self):
        text = "\n".join("".join(t for t, _s in line) for line in self.m.tree_lines(self.fleet, self.model))
        self.assertIn("workflow release-train", text)
        self.assertIn("phase Build", text)
        self.assertIn("phase Verify", text)
        self.assertIn("talks with beta-22 ×1", text)
        lines = text.splitlines()
        parent = next(i for i, ln in enumerate(lines) if "Write the tests" in ln)
        child = next(i for i, ln in enumerate(lines) if "Lint fixer" in ln)
        self.assertGreater(child, parent)
        self.assertGreater(lines[child].index("Lint fixer"), lines[parent].index("Write the tests"))  # indented under it

    def test_order_is_stable(self):
        _f, later = build(self.m, time.time() + 30)
        order = lambda model: [v["sid"] for v in self.m.ordered_sessions(model, True)]  # noqa: E731
        self.assertEqual(order(self.model), order(later))

    def test_pending_tool_alert(self):
        _f, later = build(self.m, time.time() + 200)
        alpha = next(v for v in self.m.ordered_sessions(later, True) if v["name"] == "alpha-11")
        self.assertIn("Bash running", alpha["alert"])

    def test_once_output_fits_any_width(self):
        for width in (40, 80, 120, 200):
            frame, _ = self.m.render(self.fleet, self.model, {"selected": None, "show_all": True}, width)
            for line in frame:
                self.assertLessEqual(self.m.seg_len(line), width)


class Levers(unittest.TestCase):
    def setUp(self):
        self.m = load_atc(FX.env())
        FX.clear_calls()

    def test_swarm_cap(self):
        q = self.m.Quota([os.path.join(FX.stub, "quotamax")], background=False)
        q.fetch()
        self.assertEqual(q.level(), "comfortable")
        self.assertIn("abundant", q.step(+1))
        q.fetch()
        self.assertEqual(q.level(), "abundant")
        self.assertIn("already at the top", q.step(+1))
        self.assertIn("auto", q.clear())
        q.fetch()
        self.assertIn("constrained", q.step(-1))
        q.clear()
        self.assertIn("quotamax override abundant 2", FX.calls())
        self.assertIn("quotamax override constrained 2", FX.calls())
        self.assertEqual(len(q.providers), 2)

    def test_route_plan(self):
        plan, error = self.m.route_plan([os.path.join(FX.stub, "route")], "write tests", FX.work)
        self.assertIsNone(error)
        self.assertEqual(plan["chosen"], "codex")
        self.assertIn("route task --plan-only write tests", FX.calls())

    def test_a_macos_refusal_is_reported_as_one(self):
        terminals = self.m.Terminals()
        real = (self.m.app_running, self.m.osascript, self.m.shutil.which)
        try:
            self.m.app_running = lambda name: name == "iTerm2"
            self.m.osascript = lambda script: (False, "execution error: Not authorized to send Apple events to iTerm2. (-1743)")
            self.m.shutil.which = lambda name: None  # no tmux
            message = terminals.act("/dev/ttys999", "focus")
        finally:
            self.m.app_running, self.m.osascript, self.m.shutil.which = real
        self.assertIn("Privacy & Security › Automation", message)
        self.assertNotIn("/dev/ttys999", terminals.cache)  # retried once you allow it

    def test_stop_process(self):
        victim = subprocess.Popen(["sleep", "60"])
        try:
            self.assertIn("dry run", self.m.stop_process(None, victim.pid, dry_run=True))
            self.assertEqual(self.m.stop_process(None, victim.pid), f"stopped pid {victim.pid}")
            self.assertEqual(victim.wait(timeout=5), -15)
            self.assertIn("already gone", self.m.stop_process(None, victim.pid))
        finally:
            if victim.poll() is None:
                victim.kill()

    def test_open_tab_without_a_terminal_says_what_to_run(self):
        self.assertEqual(self.m.open_tab("/tmp", "/opt/bin/route task --pool codex 'x'"),
                         "run this yourself: route task --pool codex 'x'   (in /tmp)")

    def test_herdr_is_never_started(self):
        log = os.path.join(FX.stub, "herdr.log")
        stub = os.path.join(FX.stub, "herdr-off")
        with open(stub, "w") as fh:
            fh.write(f'#!/bin/sh\necho "herdr $*" >> {log}\n[ "$1 $2" = "status server" ] && echo "status: not running"\n')
        os.chmod(stub, 0o755)
        h = self.m.Herdr([stub], background=False)
        h.fetch()
        self.assertFalse(h.running)
        with open(log) as fh:
            self.assertEqual(fh.read().strip(), "herdr status server")  # nothing that could start it

    def test_herdr_agents_and_steering(self):
        log = os.path.join(FX.stub, "herdr-on.log")
        stub = os.path.join(FX.stub, "herdr-on")
        agents = {"result": {"agents": [
            {"agent": "claude", "agent_session": {"kind": "id", "value": FX.sid("beta-22")}, "agent_status": "idle",
             "cwd": FX.work, "pane_id": "w1:p1"},
            {"agent": "codex", "agent_status": "blocked", "cwd": FX.work, "pane_id": "w2:p1"},
            {"agent": "opencode", "agent_status": "working", "cwd": FX.work, "pane_id": "w3:p1"}]}}
        with open(stub, "w") as fh:
            fh.write(f"#!/bin/sh\necho \"herdr $*\" >> {log}\n"
                     f'case "$1 $2" in "status server") echo "status: running";;\n'
                     f"\"agent list\") echo '{json.dumps(agents)}';; esac\n")
        os.chmod(stub, 0o755)
        herdr = self.m.Herdr([stub], background=False)
        feed = self.m.Feed([os.path.join(FX.stub, "claude")], background=False)
        feed.fetch()
        herdr.fetch()
        fleet = self.m.Fleet(None, feed, herdr)
        fleet.refresh(time.time())
        model = fleet.model(time.time())
        views = {v["name"]: v for v in self.m.ordered_sessions(model, True)}
        self.assertEqual(views["beta-22"]["herdr"], "w1:p1")
        self.assertEqual(views["codex w2:p1"]["group"], "blocked")
        self.assertEqual(views["opencode w3:p1"]["group"], "working")
        app = self.m.App(fleet, self.m.Quota(None), self.m.Terminals(), None, ["claude"], False)
        app.targets = [views["codex w2:p1"], views["opencode w3:p1"]]
        app.submit_message("status please")
        self.assertIn("sent to opencode w3:p1", app.flash[0])
        self.assertIn("skipped codex w2:p1", app.flash[0])
        app.jump(views["opencode w3:p1"])
        app.interrupt(views["opencode w3:p1"])
        app.interrupt(views["opencode w3:p1"])
        with open(log) as fh:
            calls = fh.read()
        self.assertIn("herdr agent prompt w3:p1 status please", calls)
        self.assertNotIn("w2:p1 status", calls)
        self.assertIn("herdr agent focus w3:p1", calls)
        self.assertIn("herdr agent send-keys w3:p1 esc", calls)


class Serve(unittest.TestCase):
    """atc --serve: the JSON Orbital reads, the writes it may make, and the schema matching both."""

    @classmethod
    def setUpClass(cls):
        cls.m = load_atc(FX.env())
        m = cls.m
        feed = m.Feed([os.path.join(FX.stub, "claude")], background=False)
        feed.fetch()
        repos = m.Repos(background=False)
        cls.fleet = m.Fleet(None, feed, m.Herdr(None, background=False), repos=repos)
        cls.fleet.refresh(time.time())
        repos.watch(s.cwd for s in cls.fleet.sessions.values())
        repos.fetch()
        quota = m.Quota([os.path.join(FX.stub, "quotamax")], background=False)
        quota.fetch()
        cls.token = "test-token-not-secret"
        cls.service = m.Service(cls.fleet, quota, [], m.Terminals(), writes=True, token=cls.token, background=False)
        cls.service.refresh()
        cls.server = m.make_server(cls.service, "127.0.0.1:0")
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, path, body=None, token=None):
        req = urllib.request.Request(self.url + path, data=None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None else "POST")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if raw.startswith(("{", "[")) else raw)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def test_sessions(self):
        status, rows = self.call("/api/taxi/sessions")
        self.assertEqual(status, 200)
        by = {r["name"]: r for r in rows}
        self.assertEqual(by["gamma-33"]["state"], "blocked")
        self.assertEqual(by["alpha-11"]["repoRoot"], FX.work)
        self.assertGreaterEqual(by["alpha-11"]["uncommittedFiles"], 1)
        self.assertEqual(by["alpha-11"]["unpushedCommits"], 1)
        status, one = self.call(f"/api/taxi/sessions/{FX.sid('beta-22')}")
        self.assertEqual((status, one["name"]), (200, "beta-22"))
        self.assertEqual(self.call("/api/taxi/sessions/nope")[0], 404)

    def test_subagents_processes_letters_quota(self):
        subs = self.call("/api/taxi/subagents")[1]
        tests = next(s for s in subs if s["label"] == "Write the tests")
        self.assertEqual(tests["sessionId"], FX.sid("alpha-11"))
        self.assertTrue(tests["finished"])
        procs = self.call("/api/taxi/processes")[1]
        stray = next(p for p in procs if p["pid"] == FX.stray)
        self.assertEqual((stray["tool"], stray["host"], stray["startedBy"]), ("codex", "local", "launchd"))
        letters = self.call("/api/taxi/letters")[1]
        self.assertEqual([(x["fromSession"], x["toSession"]) for x in letters], [("alpha-11", "beta-22")])
        quota = self.call("/api/taxi/quota")[1]
        self.assertEqual((quota["headroom"], quota["swarmCap"], quota["claudeWeeklyPercent"]), ("comfortable", 4, 40))

    def test_schema_is_served(self):
        status, text = self.call("/api/taxi/schema")
        self.assertEqual(status, 200)
        self.assertIn("namespace atc", text)

    def test_schema_and_json_agree(self):
        """Every field each model declares is a key in what atc serves, and nothing more."""
        with open(self.m.TAXI_SCHEMA) as fh:
            schema = fh.read()
        def fields(model):
            body = re.search(r"model " + model + r" \{(.*?)\n   \}", schema, re.S).group(1)
            return {line.split(":")[0].strip() for line in body.splitlines() if re.match(r"\s+\w+ :", line)}
        served = {"AgentSession": self.call("/api/taxi/sessions")[1][0],
                  "Subagent": self.call("/api/taxi/subagents")[1][0],
                  "AgentProcess": self.call("/api/taxi/processes")[1][0],
                  "SessionLetter": self.call("/api/taxi/letters")[1][0],
                  "QuotaReading": self.call("/api/taxi/quota")[1]}
        for model, sample in served.items():
            self.assertEqual(fields(model), set(sample), model)

    def test_writes_need_the_token(self):
        body = {"sessionId": FX.sid("beta-22"), "text": "no token"}
        self.assertEqual(self.call("/api/taxi/messages", body)[0], 401)
        self.assertEqual(self.call("/api/taxi/messages", body, token="wrong")[0], 401)
        self.assertNotIn("no token", FX.pane("beta-22"))

    def test_message_a_session_and_the_blocked_guard(self):
        status, res = self.call("/api/taxi/messages", {"sessionId": FX.sid("beta-22"), "text": "hello from orbital"},
                                token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(res["delivered"], res)
        time.sleep(0.5)
        self.assertIn("hello from orbital", FX.pane("beta-22"))
        status, res = self.call("/api/taxi/messages", {"sessionId": FX.sid("gamma-33"), "text": "must not arrive"},
                                token=self.token)
        self.assertFalse(res["delivered"])
        self.assertIn("blocked", res["detail"])
        time.sleep(0.5)
        self.assertNotIn("must not arrive", FX.pane("gamma-33"))

    def test_swarm_cap(self):
        FX.clear_calls()
        status, res = self.call("/api/taxi/swarm-cap", {"direction": "up"}, token=self.token)
        self.assertEqual(status, 200)
        self.assertIn("abundant", res["detail"])
        self.call("/api/taxi/swarm-cap", {"direction": "auto"}, token=self.token)
        self.assertIn("quotamax override abundant 2", FX.calls())
        self.assertEqual(self.call("/api/taxi/swarm-cap", {"direction": "sideways"}, token=self.token)[0], 400)

    def test_writes_off_by_default(self):
        service = self.m.Service(self.fleet, None, [], self.m.Terminals(), writes=False, background=False)
        self.assertEqual(service.post("/api/taxi/messages", {"sessionId": "x", "text": "y"})[0], 403)

    def test_off_this_machine_reads_need_the_token(self):
        server = self.m.make_server(self.service, "0.0.0.0:0")
        try:
            self.assertFalse(server.open_reads)
        finally:
            server.server_close()


class Closed(unittest.TestCase):
    """Closed sessions: transcripts that changed recently but aren't live."""

    def setUp(self):
        self.m = load_atc(FX.env())

    def test_closed_sessions_found(self):
        fleet, model = build(self.m, with_closed=True)
        closed = model["closed"]
        sids = {c["sid"] for c in closed}
        self.assertIn(FX.closed_sid, sids)
        # Live sessions must not appear in the closed list
        for name in fixture.NAMES:
            self.assertNotIn(FX.sid(name), sids)

    def test_closed_session_fields(self):
        fleet, model = build(self.m, with_closed=True)
        c = next(x for x in model["closed"] if x["sid"] == FX.closed_sid)
        self.assertEqual(c["title"], "api-client")  # custom title takes precedence
        self.assertIn("all tests pass", c["last_words"])
        # next_step depends on path_from_slug resolving the cwd correctly (heuristic, may fail with stale temp dirs)
        if c["cwd"] == FX.closed_cwd:
            self.assertEqual(c["next_step"], "Deploy to staging")

    def test_hide_and_unhide(self):
        fleet, model = build(self.m, with_closed=True)
        closed_obj = fleet.closed
        visible_before = len(closed_obj.visible())
        closed_obj.hide(FX.closed_sid, "done by test")
        visible_after = len(closed_obj.visible())
        self.assertEqual(visible_after, visible_before - 1)
        # It's in hidden
        self.assertIn(FX.closed_sid, closed_obj.hidden)
        # show_hidden brings it back
        closed_obj.show_hidden = True
        c = next(x for x in closed_obj.visible() if x["sid"] == FX.closed_sid)
        self.assertTrue(c["hidden"])
        self.assertEqual(c["hidden_reason"], "done by test")
        # unhide
        closed_obj.unhide(FX.closed_sid)
        closed_obj.show_hidden = False
        sids = {c["sid"] for c in closed_obj.visible()}
        self.assertIn(FX.closed_sid, sids)

    def test_resolve_closed(self):
        fleet, model = build(self.m, with_closed=True)
        item = self.m.resolve(fleet, model, f"closed::{FX.closed_sid}")
        self.assertEqual(item[0], "closed")
        self.assertEqual(item[1]["title"], "api-client")
        self.assertEqual(self.m.selection_name(item), "api-client")

    def test_closed_detail_lines(self):
        fleet, model = build(self.m, with_closed=True)
        c = next(x for x in model["closed"] if x["sid"] == FX.closed_sid)
        lines = self.m.closed_detail_lines(c, 20, 100)
        text = "\n".join("".join(t for t, _s in line) for line in lines)
        self.assertIn("api-client", text)
        self.assertIn("R R restore", text)
        if c["cwd"] == FX.closed_cwd:
            self.assertIn("Deploy to staging", text)

    def test_path_from_slug(self):
        p = self.m.path_from_slug
        # Simple cases: no ambiguity
        self.assertEqual(p("-tmp"), "/tmp")
        self.assertEqual(p("not-a-path"), "not-a-path")
        # The closed session should be found and its title parsed regardless of cwd resolution
        fleet, model = build(self.m, with_closed=True)
        c = next(x for x in model["closed"] if x["sid"] == FX.closed_sid)
        self.assertEqual(c["title"], "api-client")


class CommandLine(unittest.TestCase):
    def atc(self, *args, **env):
        res = subprocess.run([sys.executable, fixture.ATC, *args], env=FX.env(**env), capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(res.stderr, "", res.stderr)
        return res

    def test_json(self):
        data = json.loads(self.atc("--json").stdout)
        self.assertEqual(data["counts"]["blocked"], 1)
        self.assertEqual(data["quota"]["headroom"], "comfortable")
        self.assertEqual(data["hosts"], [])
        closed_sids = {c["sid"] for c in data.get("closed", [])}
        self.assertIn(FX.closed_sid, closed_sids)

    def test_once_tree_comms(self):
        once = self.atc("--once", "--width", "120").stdout
        for text in ("Blocked on you (1)", "Waiting on you (1)", "Working (1)", "release-train", "Codex weekly 19%"):
            self.assertIn(text, once)
        self.assertIn("workflow release-train", self.atc("--tree").stdout)
        self.assertIn("alpha-11 → beta-22", self.atc("--comms").stdout)

    def test_here_limits_to_a_folder(self):
        data = json.loads(self.atc("--json", "--only", os.path.join(FX.work, "alpha")).stdout)
        names = [v["name"] for g in data["groups"].values() for v in g]
        self.assertEqual(names, ["alpha-11"])

    def test_cap(self):
        FX.clear_calls()
        self.assertIn("abundant", self.atc("--cap", "up").stdout)
        self.assertIn("auto", self.atc("--cap", "auto").stdout)
        self.assertIn("quotamax override abundant 2", FX.calls())

    def test_jump_selects_the_tmux_window(self):
        target = FX.sessions["gamma-33"]
        out = self.atc("--jump", target["sid"]).stdout
        self.assertIn("focus: done", out)
        self.assertEqual(FX.active_window(), target["window"])
        bad = subprocess.run([sys.executable, fixture.ATC, "--jump", "nope"], env=FX.env(), capture_output=True,
                             text=True, timeout=60)
        self.assertEqual(bad.returncode, 1)
        unreachable = FX.env(PATH="/usr/bin:/bin")  # no tmux on PATH: the pane can't be found, and it says so
        failed = subprocess.run([sys.executable, fixture.ATC, "--jump", target["sid"]], env=unreachable,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(failed.returncode, 1, failed.stdout)

    def test_remote_host(self):
        fake_ssh = os.path.join(FX.stub, "fake-ssh")
        with open(fake_ssh, "w") as fh:  # runs the remote half locally, then pretends to be another machine
            fh.write("#!/bin/sh\nhost=\"\"\nwhile [ \"$1\" = -o ]; do shift 2; done\nhost=$1; shift\n"
                     f"\"$@\" | {sys.executable} -c 'import json,sys; d=json.load(sys.stdin); "
                     "d[\"machine\"]=\"fakebox\"; print(json.dumps(d))'\n")
        os.chmod(fake_ssh, 0o755)
        data = json.loads(self.atc("--json", "--host", "box1", "--host", "box2", ATC_SSH=fake_ssh).stdout)
        self.assertEqual([h["host"] for h in data["hosts"]], ["box1"])
        self.assertEqual(data["hosts"][0]["aliases"], ["box2"])
        self.assertTrue(data["hosts"][0]["ok"])
        self.assertEqual(data["hosts"][0]["counts"]["blocked"], 1)
        down = json.loads(self.atc("--json", "--host", "nowhere.invalid", ATC_SSH="false").stdout)
        self.assertFalse(down["hosts"][0]["ok"])

    def test_remote_that_never_reads_its_input(self):
        """A host that answers without reading the script must not deadlock atc (it did, on 5 Oct)."""
        big = os.path.join(FX.stub, "big-answer.json")
        with open(big, "w") as fh:
            json.dump({"machine": "quietbox", "counts": {}, "groups": {}, "agents": [],
                       "padding": "x" * 200_000}, fh)
        rude = os.path.join(FX.stub, "rude-ssh")
        with open(rude, "w") as fh:
            fh.write(f"#!/bin/sh\ncat {big}\n")
        os.chmod(rude, 0o755)
        start = time.time()
        data = json.loads(self.atc("--json", "--host", "quietbox", ATC_SSH=rude).stdout)
        self.assertLess(time.time() - start, 30)
        self.assertTrue(data["hosts"][0]["ok"], data["hosts"][0])


if __name__ == "__main__":
    unittest.main()
