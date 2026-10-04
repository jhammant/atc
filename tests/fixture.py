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
from datetime import datetime, timezone

ATC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "atc.py")
TMUX = shutil.which("tmux")
NAMES = ("alpha-11", "beta-22", "gamma-33", "delta-44")


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


def result(ago, tid):
    return {"type": "user", "timestamp": iso(ago),
            "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}}


def text(t):
    return {"type": "text", "text": t}


def user(ago, t):
    return {"type": "user", "timestamp": iso(ago), "message": {"content": t}}


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
        os.makedirs(self.stub)
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
            assistant(800, tool("t1", "Edit", file_path=os.path.join(a["cwd"], "src", "app.py"))),
            result(799, "t1"),
            assistant(700, tool("t2", "SendMessage", to="beta-22", summary="Ask beta to review the API",
                                message="Please review the API before we ship")),
            result(699, "t2"),
            assistant(5, tool("t3", "Bash", command="pytest -q", description="Run the test suite")),
        ])
        sub = os.path.join(adir, a["sid"], "subagents")
        helpers = [
            ("a111111111111111", {"agentType": "general-purpose", "description": "Write the tests", "spawnDepth": 1},
             [user(600, "Write unit tests for app.py and report the pass count"),
              assistant(500, tool("h1", "Write", file_path=os.path.join(a["cwd"], "tests", "test_app.py"))),
              result(499, "h1"),
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
            {"type": "queue-operation", "operation": "enqueue", "timestamp": iso(690),
             "content": f'<cross-session-message from="uds:/tmp/cc-socks-test/{a["pid"]}.sock" from-name="alpha-11">'
                        "Please review the API before we ship</cross-session-message>"},
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

    # -- use

    def env(self, **extra):
        env = dict(os.environ)
        env.update({
            "CLAUDE_CONFIG_DIR": self.claude_home, "ATC_CLAUDE": os.path.join(self.stub, "claude"),
            "ATC_QUOTAMAX": os.path.join(self.stub, "quotamax"), "ATC_ROUTE": os.path.join(self.stub, "route"),
            "ATC_HERDR": "", "ATC_HOSTS": "", "ATC_TERMINAL": "none", "TERM": "xterm-256color",
            "HOME": os.path.join(self.root, "home"),
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
