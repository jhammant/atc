"""A fake Claude Code home with live (dummy) sessions in every state, for atc's tests.

Builds, in a temp folder:
  home/.claude/sessions/*.json    the registry, pointing at real processes
  home/.claude/projects/...       transcripts, subagents (one nested), a two-phase workflow, letters
  stub/                           stand-ins for `claude agents --json`, quotamax, route and herdr, each logging calls
and runs three dummy `claude` processes (sleep under that name) in a detached tmux session, one window each,
so atc can find their terminals and steering can be checked by reading what lands in those panes.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest
from datetime import datetime, timezone

ATC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "atc.py")
TMUX = shutil.which("tmux")
NAMES = ("alpha-11", "beta-22", "gamma-33", "delta-44")
AMAIL_ALPHA = "alpha-1111@owner.amail.example.com"
AMAIL_BETA = "beta-2222@owner.amail.example.com"
AMAIL_HELPER = "helper-3333@owner.amail.example.com"
AMAIL_OTHER = "reviewer-4444@other.amail.example.com"
AMAIL_BODY = "API review by a-mail\nCheck the error paths."


def iso(seconds_ago):
    return datetime.fromtimestamp(time.time() - seconds_ago, timezone.utc).isoformat().replace("+00:00", "Z")


def slug(path):
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def write_jsonl(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def assistant(ago, *blocks, stop="tool_use"):
    return {"type": "assistant", "timestamp": iso(ago), "message": {"stop_reason": stop, "content": list(blocks)}}


def tool(tid, name, **inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def result(ago, tid, content="ok"):
    return {"type": "user", "timestamp": iso(ago),
            "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": content}]}}


def text(t):
    return {"type": "text", "text": t}


def user(ago, t):
    return {"type": "user", "timestamp": iso(ago), "message": {"content": t}}


def amail_item(mid="mail-api", frm=AMAIL_ALPHA, to=AMAIL_BETA, body=AMAIL_BODY):
    return {"delivery_id": 1, "kind": "message", "message": {
        "id": mid, "thread_id": "thread-api", "thread_kind": "direct", "seq": 1,
        "from": frm, "via": "direct", "sender_kind": "agent", "parts": [text(body)], "encrypted": False},
        "policy": {}, "provenance": {"to": to, "verified_sender": True}, "status": "delivered"}


def amail_inbox(*items):
    return json.dumps({"count": len(items), "items": list(items), "owner_decisions": []})


STUB_CLAUDE = '''#!/usr/bin/env python3
import json, os, sys
d = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(d, "calls.log"), "a") as fh:
    fh.write("claude " + " ".join(sys.argv[1:]) + "\\n")
with open(os.path.join(d, "stdin.log"), "a") as fh:  # the real claude would read keystrokes from a tty stdin
    fh.write(f"claude {os.isatty(0)}\\n")
if sys.argv[1:3] == ["agents", "--json"]:
    print(open(os.path.join(d, "agents.json")).read())
'''

STUB_QUOTAMAX = '''#!/usr/bin/env python3
import json, os, sys
d = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(d, "calls.log"), "a") as fh:
    fh.write("quotamax " + " ".join(sys.argv[1:]) + "\\n")
level_file = os.path.join(d, "level")
args = sys.argv[1:]
if args[:1] == ["agent"]:
    level = open(level_file).read().strip() if os.path.exists(level_file) else None
    print(json.dumps({"ok": True, "headroom": "comfortable", "override": {"level": level, "until": None} if level else None,
                      "advice": {"parallelism": 4}, "session": {"percentUsed": 12}, "weekly": {"effectivePercent": 40}}))
elif args[:2] == ["providers", "--json"]:
    print(json.dumps([{"id": "codex", "label": "Codex (test)", "configured": True, "ok": True,
                       "limits": [{"label": "weekly", "percent": 19}]},
                      {"id": "deepseek", "label": "DeepSeek", "configured": True, "ok": True, "limits": [],
                       "balanceUsd": 9.5, "low": False}]))
elif args[:1] == ["override"]:
    if args[1] == "clear":
        os.path.exists(level_file) and os.remove(level_file)
    else:
        open(level_file, "w").write(args[1])
'''

STUB_AMAIL = '''#!/usr/bin/env python3
import os, sys
d = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(d, "calls.log"), "a") as fh:
    fh.write("amail " + " ".join(sys.argv[1:]) + "\\n")
if sys.argv[1:] != ["status", "--json"]:
    sys.exit(2)
if os.path.exists(os.path.join(d, "amail-fail")):
    print("offline", file=sys.stderr)
    sys.exit(1)
with open(os.path.join(d, "amail-status.json")) as fh:
    print(fh.read())
'''


def amail_status():
    return {"server": "https://amail.example.com", "address": "owner@amail.example.com", "health": {"ok": True},
            "inbox": {"waiting": 4, "more": True}, "owner": {"requests": 3, "approvals": 2, "proposals": 1},
            "device": {"awaitingApproval": 2, "approvedByOwner": False}}


def stub_amail(directory):
    path = os.path.join(directory, "amail")
    with open(path, "w") as fh:
        fh.write(STUB_AMAIL)
    os.chmod(path, 0o755)
    with open(os.path.join(directory, "amail-status.json"), "w") as fh:
        json.dump(amail_status(), fh)


STUB_ROUTE = '''#!/usr/bin/env python3
import json, os, sys
d = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(d, "calls.log"), "a") as fh:
    fh.write("route " + " ".join(sys.argv[1:]) + "\\n")
print(json.dumps({"chosen": "codex", "shape": "coding:test", "tier": "trivial", "why": "posterior",
                  "removed_by_quota": []}))
print("dispatch: plan only (nothing launched)")
'''


class Fixture:
    def __init__(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="atc-test-"))
        self.claude_home = os.path.join(self.root, "home", ".claude")
        self.stub = os.path.join(self.root, "stub")
        self.work = os.path.join(self.root, "work")
        self.tmux_session = f"atc-fx-{os.getpid()}"
        self.sessions = {}  # name -> {"sid", "pid", "tty", "window", "cwd"}

    # -- setup

    def start(self):
        try:
            probe = subprocess.run(["ps", "-p", str(os.getpid()), "-o", "pid="], capture_output=True, text=True)
        except PermissionError as exc:
            shutil.rmtree(self.root, ignore_errors=True)
            raise unittest.SkipTest(f"live-process fixture needs ps; sandbox denied it: {exc}") from exc
        if probe.returncode and any(s in probe.stderr.lower() for s in ("operation not permitted", "permission denied")):
            shutil.rmtree(self.root, ignore_errors=True)
            raise unittest.SkipTest(f"live-process fixture needs ps: {probe.stderr.strip()}")
        os.makedirs(self.stub)
        stub_amail(self.stub)
        for name, body in (("claude", STUB_CLAUDE), ("quotamax", STUB_QUOTAMAX), ("route", STUB_ROUTE)):
            path = os.path.join(self.stub, name)
            with open(path, "w") as fh:
                fh.write(body)
            os.chmod(path, 0o755)
        sleeper_dir = os.path.join(self.root, "bin")
        os.makedirs(sleeper_dir)
        os.symlink(shutil.which("sleep"), os.path.join(sleeper_dir, "claude"))  # a process named claude
        cwds = {"beta-22": self.work, "alpha-11": os.path.join(self.work, "alpha"),
                "gamma-33": os.path.join(self.work, "gamma"), "delta-44": os.path.join(self.root, "parked")}
        for cwd in cwds.values():
            os.makedirs(cwd, exist_ok=True)
        sleeper = os.path.join(sleeper_dir, "claude")
        first = True
        for name in NAMES:
            cmd = f"exec {sleeper} 3600"
            if first:
                subprocess.run([TMUX, "new-session", "-d", "-s", self.tmux_session, "-x", "120", "-y", "30",
                                "-c", cwds[name], cmd], check=True)
                first = False
            else:
                subprocess.run([TMUX, "new-window", "-t", self.tmux_session, "-c", cwds[name], cmd], check=True)
        out = subprocess.run([TMUX, "list-panes", "-s", "-t", self.tmux_session, "-F",
                              "#{window_index} #{pane_pid} #{pane_tty}"], capture_output=True, text=True, check=True).stdout
        panes = [line.split() for line in out.splitlines()]
        for (window, pid, tty), name in zip(panes, NAMES):
            self.sessions[name] = {"sid": self.sid(name), "pid": int(pid), "tty": tty, "window": window,
                                   "cwd": cwds[name]}
        self.write_registry()
        self.write_transcripts()
        self.write_agents_json()
        self.make_repo()
        self.write_closed_transcript()
        codex = os.path.join(sleeper_dir, "codex")  # an agent process outside every session...
        os.symlink(shutil.which("sleep"), codex)
        out = subprocess.run(["/bin/sh", "-c", f"{codex} 3600 >/dev/null 2>&1 </dev/null & echo $!"],
                             capture_output=True, text=True, check=True)
        self.stray = int(out.stdout.strip())  # ...orphaned to launchd, like a forgotten one
        return self

    def make_repo(self):
        """The work folder is a git repo with one uncommitted file: unsaved work atc should flag."""
        git = ["git", "-C", self.work, "-c", "user.email=t@example.com", "-c", "user.name=t"]
        subprocess.run(["git", "init", "-q", self.work], check=True)
        with open(os.path.join(self.work, "README.md"), "w") as fh:
            fh.write("fixture\n")
        subprocess.run(git + ["add", "README.md"], check=True)
        subprocess.run(git + ["commit", "-q", "-m", "init"], check=True)
        with open(os.path.join(self.work, "notes.txt"), "w") as fh:
            fh.write("not committed yet\n")

    @staticmethod
    def sid(name):
        n = {"alpha-11": 1, "beta-22": 2, "gamma-33": 3, "bg": 4, "delta-44": 5}[name]
        return f"0000000{n}-0000-4000-8000-00000000000{n}"

    def write_registry(self):
        os.makedirs(os.path.join(self.claude_home, "sessions"), exist_ok=True)
        status = {"alpha-11": ("busy", 20), "beta-22": ("idle", 600), "gamma-33": ("waiting", 30),
                  "delta-44": ("idle", 13 * 3600)}
        for i, (name, s) in enumerate(self.sessions.items()):
            st, ago = status[name]
            data = {"pid": s["pid"], "sessionId": s["sid"], "cwd": s["cwd"], "name": name, "status": st,
                    "statusUpdatedAt": int((time.time() - ago) * 1000), "startedAt": int((time.time() - 3600 + i) * 1000),
                    "messagingSocketPath": f"/tmp/cc-socks-test/{s['pid']}.sock", "version": "9.9.9"}
            with open(os.path.join(self.claude_home, "sessions", f"{s['pid']}.json"), "w") as fh:
                json.dump(data, fh)

    def write_agents_json(self, overrides=None):
        status = {"alpha-11": ("busy", None), "beta-22": ("idle", None), "gamma-33": ("waiting", "permission prompt"),
                  "delta-44": ("idle", None)}
        status.update(overrides or {})
        entries = []
        for i, (name, s) in enumerate(self.sessions.items()):
            st, waiting_for = status[name]
            entry = {"pid": str(s["pid"]), "cwd": s["cwd"], "kind": "interactive", "name": name,
                     "sessionId": s["sid"], "startedAt": str(int((time.time() - 3600 + i) * 1000)), "status": st}
            if waiting_for:
                entry["waitingFor"] = waiting_for
            entries.append(entry)
        entries.append({"id": "deadbeef", "cwd": os.path.join(self.work, "nightly"), "kind": "background",
                        "sessionId": self.sid("bg"), "name": "Nightly refactor", "state": "blocked",
                        "startedAt": str(int((time.time() - 7200) * 1000))})
        with open(os.path.join(self.stub, "agents.json"), "w") as fh:
            json.dump(entries, fh)

    def transcript_dir(self, name):
        return os.path.join(self.claude_home, "projects", slug(self.sessions[name]["cwd"]))

    def write_transcripts(self):
        a, b, g = self.sessions["alpha-11"], self.sessions["beta-22"], self.sessions["gamma-33"]
        adir = self.transcript_dir("alpha-11")
        write_jsonl(os.path.join(adir, f"{a['sid']}.jsonl"), [
            user(900, "Build the release train for the API"),
            {"type": "ai-title", "aiTitle": "Alpha feature work"},
            assistant(850, tool("mail-who", "mcp__amail__amail_whoami")),
            result(849, "mail-who", json.dumps({"default_address": AMAIL_ALPHA,
                   "addresses": [{"address": AMAIL_ALPHA, "kind": "session"},
                                 {"address": "alias-1111@owner.amail.example.com", "kind": "alias"}],
                   "agent": {"name": "fixture-alpha"}})),
            assistant(800, tool("t1", "Edit", file_path=os.path.join(a["cwd"], "src", "app.py"))),
            result(799, "t1"),
            assistant(700, tool("t2", "SendMessage", to="beta-22", summary="Ask beta to review the API",
                                message="Please review the API before we ship")),
            result(699, "t2"),
            assistant(680, tool("mail-send", "mcp__amail__amail_send", to=AMAIL_BETA, text=AMAIL_BODY,
                                thread="thread-api", subject="Review")),
            result(679, "mail-send"),
            assistant(5, tool("t3", "Bash", command="pytest -q", description="Run the test suite")),
        ])
        sub = os.path.join(adir, a["sid"], "subagents")
        helpers = [
            ("a111111111111111", {"agentType": "general-purpose", "description": "Write the tests", "spawnDepth": 1},
             [user(600, "Write unit tests for app.py and report the pass count"),
              assistant(500, tool("h1", "Write", file_path=os.path.join(a["cwd"], "tests", "test_app.py"))),
              result(499, "h1"),
              assistant(490, tool("helper-who", "mcp__amail__amail_whoami")),
              result(489, "helper-who", [text(json.dumps({"default_address": AMAIL_HELPER}))]),
              assistant(480, tool("helper-send", "mcp__amail__amail_send", to=AMAIL_OTHER,
                                  text="Helper review by a-mail")),
              result(479, "helper-send"),
              assistant(400, text("All 12 tests pass"), stop="end_turn")], sub),
            ("a222222222222222", {"agentType": "general-purpose", "description": "Lint fixer", "spawnDepth": 2,
                                  "parentAgentId": "a111111111111111", "model": "haiku"},
             [user(300, "Fix the lint errors in tests/"),
              assistant(3, tool("h2", "Bash", command="ruff check tests", description="Lint the tests"))], sub),
        ]
        wf = os.path.join(sub, "workflows", "wf_abc123-001")
        helpers += [
            ("a333333333333333", {"agentType": "workflow-subagent", "description": "API client", "workflowPhase": "Build"},
             [user(250, "Build the API client"), assistant(200, text("API client done"), stop="end_turn")], wf),
            ("a444444444444444", {"agentType": "workflow-subagent", "description": "Smoke tester", "workflowPhase": "Verify"},
             [user(100, "Smoke test staging"),
              assistant(2, tool("w4", "Bash", command="curl -s staging/health", description="Hit the staging health check"))], wf),
        ]
        for hid, meta, records, folder in helpers:
            write_jsonl(os.path.join(folder, f"agent-{hid}.jsonl"), records)
            with open(os.path.join(folder, f"agent-{hid}.meta.json"), "w") as fh:
                json.dump(meta, fh)
        write_jsonl(os.path.join(wf, "journal.jsonl"), [
            {"type": "launched"},
            {"type": "started", "agentId": "a333333333333333", "label": "build:api", "phase": "Build"},
            {"type": "result", "agentId": "a333333333333333", "result": "{}"},
            {"type": "started", "agentId": "a444444444444444", "label": "verify:smoke", "phase": "Verify"},
        ])
        scripts = os.path.join(adir, a["sid"], "workflows", "scripts")
        os.makedirs(scripts)
        with open(os.path.join(scripts, "release-train-wf_abc123-001.js"), "w") as fh:
            fh.write(textwrap.dedent("""\
                export const meta = {
                  name: 'release-train',
                  description: 'Build then verify',
                  phases: [{ title: 'Build' }, { title: 'Verify' }],
                }
                """))
        write_jsonl(os.path.join(self.transcript_dir("beta-22"), f"{b['sid']}.jsonl"), [
            user(1200, "Draft the changelog"),
            {"type": "ai-title", "aiTitle": "Changelog"},
            {"type": "custom-title", "customTitle": "changelog-drafter"},
            assistant(800, tool("mail-session", "mcp__amail__amail_session")),
            result(799, "mail-session", [text(json.dumps({"address": AMAIL_BETA}))]),
            {"type": "queue-operation", "operation": "enqueue", "timestamp": iso(690),
             "content": f'<cross-session-message from="uds:/tmp/cc-socks-test/{a["pid"]}.sock" from-name="alpha-11">'
                        "Please review the API before we ship</cross-session-message>"},
            assistant(650, tool("mail-inbox", "mcp__amail__amail_inbox", wait_seconds=5)),
            result(645, "mail-inbox", amail_inbox(amail_item())),
            assistant(630, tool("mail-inbox-again", "mcp__amail__amail_inbox", wait_seconds=5)),
            result(625, "mail-inbox-again", [text(amail_inbox(amail_item(), amail_item(
                "mail-notes", AMAIL_OTHER, AMAIL_BETA, "Review notes by a-mail\nThe error paths look good.")))]),
            assistant(610, text("Done. Want me to ship it?"), stop="end_turn"),
        ])
        d = self.sessions["delta-44"]
        write_jsonl(os.path.join(self.transcript_dir("delta-44"), f"{d['sid']}.jsonl"), [
            user(13 * 3600 + 60, "Tidy the old notes"),
            {"type": "ai-title", "aiTitle": "Old notes"},
            assistant(13 * 3600, text("Tidied; nothing else to do."), stop="end_turn"),
        ])
        write_jsonl(os.path.join(self.transcript_dir("gamma-33"), f"{g['sid']}.jsonl"), [
            user(120, "Clean up the build output"),
            {"type": "ai-title", "aiTitle": "Cleanup"},
            assistant(30, tool("g1", "Bash", command="rm -rf build", description="Delete the build folder")),
        ])

    def write_closed_transcript(self):
        """A transcript for a session that is NOT live: a closed session atc should find."""
        closed_cwd = os.path.join(self.work, "closed-proj")
        os.makedirs(closed_cwd, exist_ok=True)
        slug_name = re.sub(r"[^A-Za-z0-9]", "-", closed_cwd)
        closed_dir = os.path.join(self.claude_home, "projects", slug_name)
        closed_sid = "cccccccc-0000-4000-8000-000000000001"
        write_jsonl(os.path.join(closed_dir, f"{closed_sid}.jsonl"), [
            dict(user(7200, "Build the API client for the new service"), cwd=closed_cwd),  # as real transcripts do
            {"type": "ai-title", "aiTitle": "API client build"},
            {"type": "custom-title", "customTitle": "api-client"},
            assistant(3600, text("Done. The API client is ready and all tests pass."), stop="end_turn"),
        ])
        # Write a STATE.md with next steps
        with open(os.path.join(closed_cwd, "STATE.md"), "w") as fh:
            fh.write("# State\n## Next Steps\n- Deploy to staging\n- Run integration tests\n")
        self.closed_cwd = closed_cwd
        self.closed_sid = closed_sid

    # -- use

    def env(self, **extra):
        env = dict(os.environ)
        env.update({
            "CLAUDE_CONFIG_DIR": self.claude_home, "ATC_CLAUDE": os.path.join(self.stub, "claude"),
            "ATC_QUOTAMAX": os.path.join(self.stub, "quotamax"), "ATC_ROUTE": os.path.join(self.stub, "route"),
            "ATC_AMAIL": "", "ATC_APPROVAL_WAIT": "0",
            "ATC_HERDR": "", "ATC_HOSTS": "", "ATC_TERMINAL": "none", "TERM": "xterm-256color",
            "HOME": os.path.join(self.root, "home"),
            "ROUTE_STATE_DIR": os.path.join(self.root, "route"), "ATC_SWARM_HOURS": "6",
        })
        env.update(extra)
        return env

    def calls(self):
        path = os.path.join(self.stub, "calls.log")
        if not os.path.exists(path):
            return ""
        with open(path) as fh:
            return fh.read()

    def clear_calls(self):
        path = os.path.join(self.stub, "calls.log")
        if os.path.exists(path):
            os.remove(path)

    def pane(self, name):
        window = self.sessions[name]["window"]
        return subprocess.run([TMUX, "capture-pane", "-p", "-t", f"{self.tmux_session}:{window}"],
                              capture_output=True, text=True).stdout

    def active_window(self):
        return subprocess.run([TMUX, "display-message", "-p", "-t", self.tmux_session, "#{window_index}"],
                              capture_output=True, text=True).stdout.strip()

    def stop(self):
        if getattr(self, "stray", None):
            try:
                os.kill(self.stray, 9)
            except OSError:
                pass
        subprocess.run([TMUX, "kill-session", "-t", self.tmux_session], capture_output=True)
        shutil.rmtree(self.root, ignore_errors=True)


class AmailFixture(Fixture):
    """The same transcripts with a fixed process table, for comms tests without ps or tmux access."""

    def __init__(self):
        super().__init__()
        os.makedirs(self.stub)
        stub_amail(self.stub)
        for i, name in enumerate(NAMES):
            cwd = os.path.join(self.work, name)
            os.makedirs(cwd)
            self.sessions[name] = {"sid": self.sid(name), "pid": 100 + i, "tty": "", "window": str(i), "cwd": cwd}
        self.write_registry()
        self.write_transcripts()
        self.write_agents_json()
        processes = "".join(f"{s['pid']} 1 ? 02:00 claude --resume {s['sid']}\n" for s in self.sessions.values())
        for name, body in (("claude", STUB_CLAUDE), ("ps", '''#!/usr/bin/env python3
import os
with open(os.path.join(os.path.dirname(__file__), "processes.txt")) as fh:
    print(fh.read(), end="")
''')):
            path = os.path.join(self.stub, name)
            with open(path, "w") as fh:
                fh.write(body)
            os.chmod(path, 0o755)
        with open(os.path.join(self.stub, "processes.txt"), "w") as fh:
            fh.write(processes)

    def env(self, **extra):
        return super().env(PATH=self.stub + os.pathsep + os.environ["PATH"], ATC_QUOTAMAX="", ATC_ROUTE="", **extra)

    def stop(self):
        shutil.rmtree(self.root, ignore_errors=True)


class SwarmFixture:
    """Route status files plus a fake Claude home and process table; no live agents or tmux needed."""

    def __init__(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="atc-swarms-"))
        self.state = os.path.join(self.root, "route")
        self.stub = os.path.join(self.root, "stub")
        self.work = os.path.join(self.root, "work")
        self.claude_home = os.path.join(self.root, "home", ".claude")
        self.sid = "11111111-0000-4000-8000-000000000001"
        self.now = int(time.time())
        self.table = {
            100: self.proc(1, "claude --resume " + self.sid),
            110: self.proc(100, "/bin/zsh -c route swarm"),
            120: self.proc(110, "/usr/bin/python3 /tools/route.py swarm"),
            201: self.proc(120, "codex exec implement"),
            202: self.proc(120, "kimi --print review"),
            210: self.proc(201, "codex exec nested-worker"),
            400: self.proc(1, "codex exec unrelated"),
        }
        for path in (self.stub, self.work, os.path.join(self.claude_home, "sessions")):
            os.makedirs(path, exist_ok=True)
        session = {"sessionId": self.sid, "pid": 100, "name": "owner-session", "cwd": self.work,
                   "kind": "interactive", "status": "busy", "startedAt": self.at(1000)}
        self.write_json(os.path.join(self.claude_home, "sessions", self.sid + ".json"), session)
        self.write_json(os.path.join(self.stub, "agents.json"), [session])
        write_jsonl(os.path.join(self.claude_home, "projects", slug(self.work), self.sid + ".jsonl"), [
            user(600, "Run the release swarm"), assistant(10, tool("work", "Bash", command="route swarm release")),
        ])
        for name, body in (("claude", STUB_CLAUDE), ("ps", '''#!/usr/bin/env python3
import os
with open(os.path.join(os.path.dirname(__file__), "processes.txt")) as fh:
    print(fh.read(), end="")
''')):
            path = os.path.join(self.stub, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
            os.chmod(path, 0o755)
        self.write_processes()
        live = self.status("live", "release")
        live["units"] = [self.unit("alpha", "codex", "running", 201), self.unit("beta", "kimi", "running", 202),
                         self.unit("gamma", "claude", "checking"), self.unit("later", "claude", "queued"),
                         self.unit("complete", "claude", "accepted"), self.unit("broken", "kimi", "failed"),
                         self.unit("lost", "codex", "running", 299)]
        live["channel"]["messages"] = 2
        live["held"] = [{"id": "needs-context", "reason": "veto:needs-context: missing task"}]
        self.write_status(live)
        self.write_status(self.status("recent", "finished", "done", finished_ago=3600))
        self.write_status(self.status("stale", "abandoned", route_pid=999, started_ago=30 * 3600))
        self.write_status(self.status("old", "expired", "done", finished_ago=8 * 3600))
        write_jsonl(self.channel_path(), [
            {"at": self.at(15), "from": "agent.alpha@a-mail.test", "from_unit": "alpha",
             "text": "Implementation ready\nAll checks passed."},
            {"at": self.at(5), "from": "stray.worker@a-mail.test", "from_unit": None,
             "text": "Need the shared fixture"},
        ])

    @staticmethod
    def proc(ppid, command):
        return {"ppid": ppid, "command": command, "tty": "", "etime": "02:00"}

    def at(self, ago):
        return datetime.fromtimestamp(self.now - ago, timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def write_json(path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def unit(self, ident="worker", arm="codex", state="running", pid=None):
        finished = state in ("accepted", "failed")
        return {"id": ident, "arm": arm, "state": state, "pid": pid, "address": f"agent.{ident}@a-mail.test",
                "branch": f"swarm/release/{ident}", "worktree": os.path.join(self.work, "wt", ident),
                "log": os.path.join(self.state, "swarms", "live", "logs", ident + ".log"),
                "started_at": None if state == "queued" else self.at(90),
                "finished_at": self.at(30) if finished else None, "agent_rc": 0 if finished else None,
                "wall_clock_s": 60 if finished else None}

    def status(self, ident, name, state="running", route_pid=120, finished_ago=60, started_ago=600):
        return {"schema": "route-swarm-status/1", "swarm_id": ident, "name": name, "state": state,
                "exit_code": None if state == "running" else 0, "host": "claude", "repo": self.work,
                "route_pid": route_pid, "started_at": self.at(max(started_ago, finished_ago + 60)),
                "updated_at": self.at(0), "finished_at": None if state == "running" else self.at(finished_ago),
                "hub": "hub@a-mail.test", "group_id": None,
                "channel": {"messages": 0, "log": self.channel_path(ident)}, "held": [],
                "units": [self.unit(state="running" if state == "running" else "accepted")]}

    def status_path(self, ident="live"):
        return os.path.join(self.state, "swarms", ident, "status.json")

    def channel_path(self, ident="live"):
        return os.path.join(self.state, "swarms", ident, "channel.jsonl")

    def write_status(self, data):
        path = self.status_path(data["swarm_id"])
        old_mtime = os.stat(path).st_mtime_ns if os.path.exists(path) else 0
        self.write_json(path + ".tmp", data)
        os.replace(path + ".tmp", path)
        if os.stat(path).st_mtime_ns <= old_mtime:
            os.utime(path, ns=(old_mtime + 1, old_mtime + 1))

    def write_processes(self):
        with open(os.path.join(self.stub, "processes.txt"), "w", encoding="utf-8") as fh:
            for pid, proc in self.table.items():
                fh.write(f"{pid} {proc['ppid']} ? {proc['etime']} {proc['command']}\n")

    def env(self, **extra):
        return {**os.environ, "HOME": os.path.join(self.root, "home"), "CLAUDE_CONFIG_DIR": self.claude_home,
                "ROUTE_STATE_DIR": self.state, "ATC_SWARM_HOURS": "6", "PATH": self.stub + os.pathsep + os.environ["PATH"],
                "ATC_CLAUDE": os.path.join(self.stub, "claude"), "ATC_QUOTAMAX": "", "ATC_HERDR": "", "ATC_HOSTS": "",
                "ATC_AMAIL": "",
                "ATC_TERMINAL": "none", **extra}

    def stop(self):
        shutil.rmtree(self.root, ignore_errors=True)
