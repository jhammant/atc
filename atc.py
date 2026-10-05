#!/usr/bin/env python3
"""atc: air traffic control for your coding agents. One terminal view of every Claude Code session and
agent process on this machine (and your servers), with levers to steer them.

Where it looks (read-only):
  `claude agents --json`            the live sessions, interactive and background, and whether one is blocked
  ~/.claude/sessions/*.json         extra detail per session (since when, messaging address); the fallback list
  ~/.claude/projects/*/<id>.jsonl   transcripts, plus each session's subagents and workflow journals
  ps                                terminals, and agent processes (codex, kimi, claude -p ...) a session started

What it shows: who is blocked on you or waiting for you, what every busy session and its helpers are doing,
workflow phases, the whole hierarchy (session > workflow > phase > agent > nested agent > worker process),
every message (session to session, the task a session gave a helper, the helper's report back) and one
merged activity log.

Levers (each only when its tool is installed):
  enter     jump to the session's terminal tab (iTerm2, Terminal.app, tmux); a background session opens attached
  m / M     type a message into the session, or into every session in its project, as if you typed it there
  x x       interrupt the session (Esc), or stop a background session
  + / - / 0 swarm cap: `quotamax override` pins the parallel-agent budget that sessions check before fanning out
  n         new task: `route task --plan-only` shows where it should run; enter dispatches it in a new tab

Python 3.9+ standard library only.
  atc               live view          atc --once      one frame as text
  atc --json        the data as JSON   atc --here      only sessions in or under the current directory
"""
from __future__ import annotations

import argparse
import curses
import glob
import json
import locale
import os
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
import unicodedata
from datetime import datetime

CLAUDE_HOME = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")
SESSIONS_DIR = os.path.join(CLAUDE_HOME, "sessions")
PROJECTS_DIR = os.path.join(CLAUDE_HOME, "projects")
HOME = os.path.expanduser("~")

TICK_SECONDS = 1.0
FEED_EVERY = 2.0                  # `claude agents --json`
PROC_EVERY = 5.0                  # ps
QUOTA_EVERY = 60.0
BACKLOG_BYTES = 1_500_000         # how far back into a session transcript to read at start-up
HELPER_BACKLOG_BYTES = 400_000
HEAD_BYTES = 262_144              # where a helper's task (its first prompt) is looked for
HELPER_WINDOW = 2 * 3600          # helpers whose transcript changed in this window are tracked
HELPER_DONE_SHOWN = 15 * 60       # finished helpers stay in the main list this long (the tree shows them all)
WORKING_SECONDS = 60              # a helper that wrote this recently counts as working
PARKED_AFTER = 12 * 3600          # idle this long: parked rather than waiting on you
QUIET_WARN = 10 * 60              # a busy session silent this long gets flagged
PENDING_WARN = 90                 # a tool call unanswered this long gets flagged
FLASH_SECONDS = 8.0
CONFIRM_SECONDS = 3.0
LOCATE_CACHE_SECONDS = 30.0
ACTIVITY_KEEP = 1000
CAP_LEVELS = ["critical", "constrained", "comfortable", "abundant"]
CAP_SIZE = {"critical": 1, "constrained": 2, "comfortable": 4, "abundant": 8}
SESSION_COLORS = ["c0", "c1", "c2", "c3", "c4"]
WORKER_NAMES = {"codex", "kimi", "claude", "aider", "gemini", "opencode", "goose", "deepseek", "cursor-agent", "pi",
                "hermes"}
STALE_AGENT_SECONDS = 24 * 3600  # an agent process running longer than this is probably stuck or forgotten
REMOTE_EVERY = 15.0
HOSTS_FILE = os.path.expanduser("~/.config/atc/hosts")
LAUNCHERS = {"node", "python", "python3", "sh", "bash", "zsh", "env", "bun", "deno", "uv", "npx"}
PATH_NOISE = {"index.js", "cli.js", "main.js", "main.py", "dist", "bin", "lib", "src", "build", ".bin", "node_modules"}

CROSS_RE = re.compile(r"<cross-session-message\b([^>]*)>(.*?)</cross-session-message>", re.S)
ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
DOC_ATTACHMENT_RE = re.compile(r"tool|instruction|skill|listing|reminder|mcp|hook|environment", re.I)
REF_SUFFIX_RE = re.compile(r"\s*\[[0-9a-f]{4,}\]$")
AGENT_ID_RE = re.compile(r"^a[0-9a-f]{12,}$")
WF_NAME_RE = re.compile(r"""\bname:\s*['"]([^'"]{1,80})['"]""")


# ---------------------------------------------------------------- small helpers

def parse_ts(value):
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, (int, float)):
        return value / 1000.0 if value > 1e12 else float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def squash(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def first_line(text):
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        )
    return ""


def short_path(path, base=None):
    path = str(path or "")
    if base and path.startswith(base.rstrip(os.sep) + os.sep):
        return os.path.relpath(path, base)
    if path == HOME or path.startswith(HOME + os.sep):
        return "~" + path[len(HOME):]
    return path


def short_tool(name):
    name = str(name or "")
    if name.startswith("mcp__"):
        parts = name.split("__")
        server = parts[1].replace("claude_ai_", "") if len(parts) > 1 else "mcp"
        return f"{server}:{parts[-1]}" if len(parts) > 2 else server
    return name


def detail_of(name, inp, base=None):
    """A short developer-facing description of a tool call."""
    for key in ("file_path", "notebook_path"):
        if inp.get(key):
            return short_path(inp[key], base)
    if name == "SendMessage":
        return f"-> {REF_SUFFIX_RE.sub('', str(inp.get('to') or '?'))}  {inp.get('summary') or ''}".strip()
    if name == "Workflow":
        match = WF_NAME_RE.search(str(inp.get("script") or ""))
        return match.group(1) if match else str(inp.get("name") or "")
    if name == "AskUserQuestion":
        questions = inp.get("questions")
        if isinstance(questions, list) and questions and isinstance(questions[0], dict):
            return str(questions[0].get("question") or "")
    for key in ("description", "command", "pattern", "query", "skill", "url", "subject", "prompt"):
        value = inp.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for value in inp.values():
        if isinstance(value, str) and value.strip():
            return value
    return ""


def human_age(seconds):
    if seconds is None:
        return "-"
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def etime_seconds(etime):
    """ps elapsed time ([[dd-]hh:]mm:ss) in seconds."""
    days, _, clock = str(etime).rpartition("-")
    parts = [int(x) for x in clock.split(":") if x.isdigit()]
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds + (int(days) * 86400 if days.isdigit() else 0)


def within(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def run(args, cwd=None, timeout=15):
    """Run a command and capture it. It never gets the terminal as stdin: `claude agents --json` and friends would
    read the keystrokes meant for atc's screen."""
    try:
        res = subprocess.run(args, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "", str(exc)
    return res.returncode, res.stdout, res.stderr


def command_from_env(var, default):
    raw = os.environ.get(var)
    if raw is not None:
        return shlex.split(raw) if raw.strip() else None
    found = shutil.which(default)
    return [found] if found else None


def json_in(text, kind=dict):
    """The JSON object (or array) inside a command's output, ignoring any lines printed around it."""
    opener, closer = ("{", "}") if kind is dict else ("[", "]")
    start, end = text.find(opener), text.rfind(closer)
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start: end + 1])
    except ValueError:
        return None
    return value if isinstance(value, kind) else None


def read_head(path):
    """A helper's task: the first real user message in its transcript, and when it was given."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(HEAD_BYTES)
    except OSError:
        return "", None
    for raw in data.split(b"\n"):
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("type") == "user" and not rec.get("isMeta"):
            text = text_of((rec.get("message") or {}).get("content")).strip()
            if text:
                return text, parse_ts(rec.get("timestamp"))
    return "", None


# ---------------------------------------------------------------- reading transcripts

class Tail:
    """Reads only what was appended to a JSON-lines file since the last call, starting near its end."""

    def __init__(self, path, backlog):
        self.path = path
        self.backlog = backlog
        self.offset = None
        self.buf = b""
        self.mtime = 0.0

    def read_new(self):
        """Returns (records, restarted). restarted means the file was rewritten and earlier state is void."""
        try:
            st = os.stat(self.path)
        except OSError:
            return [], False
        self.mtime = st.st_mtime
        restarted = skip_partial = False
        if self.offset is None or st.st_size < self.offset:
            restarted = self.offset is not None
            self.offset = max(0, st.st_size - self.backlog)
            self.buf = b""
            skip_partial = self.offset > 0
        if st.st_size == self.offset:
            return [], restarted
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                data = fh.read(st.st_size - self.offset)
        except OSError:
            return [], restarted
        self.offset += len(data)
        if skip_partial:  # we started mid-line
            cut = data.find(b"\n")
            data = data[cut + 1:] if cut >= 0 else b""
        lines = (self.buf + data).split(b"\n")
        self.buf = lines.pop()  # keep a half-written last line for next time
        out = []
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out, restarted


class Agent:
    """One transcript: a top-level session or a helper (a subagent or a workflow agent)."""

    backlog = HELPER_BACKLOG_BYTES

    def __init__(self, path=None):
        self.tail = Tail(path, self.backlog) if path else None
        self.label = ""
        self.color = "plain"
        self.reset()

    def reset(self):
        self.action = None        # {"tool", "detail", "ts"}
        self.pending = {}         # tool_use_id -> ts
        self.last_kind = None
        self.last_text = ""       # first line of the latest text it wrote
        self.last_text_full = ""
        self.last_stop = None
        self.last_ts = None
        self.ai_title = None
        self.custom_title = None
        self.sent = []
        self.received = []

    @property
    def base(self):
        return None

    @property
    def mtime(self):
        return self.tail.mtime if self.tail else 0.0

    def attach(self, path):
        self.tail = Tail(path, self.backlog)

    def poll(self, fleet):
        if not self.tail:
            return
        recs, restarted = self.tail.read_new()
        if restarted:
            self.reset()
        for rec in recs:
            try:
                self.ingest(rec, fleet)
            except Exception:  # one odd record must never take the dashboard down
                continue

    def ingest(self, rec, fleet):
        ts = parse_ts(rec.get("timestamp"))
        if ts:
            self.last_ts = max(self.last_ts or 0.0, ts)
        kind = rec.get("type")
        if kind == "assistant":
            self.ingest_assistant(rec.get("message") or {}, ts, fleet)
        elif kind == "user":
            self.ingest_user(rec, ts)
        elif kind == "ai-title" and rec.get("aiTitle"):
            self.ai_title = squash(rec["aiTitle"])
        elif kind == "custom-title":
            title = rec.get("customTitle") or rec.get("title")
            if isinstance(title, str) and title.strip():
                self.custom_title = squash(title)
        elif kind == "attachment":
            self.ingest_attachment(rec.get("attachment"), ts)
        elif kind == "queue-operation" and rec.get("operation") == "enqueue":
            self.scan_incoming(rec.get("content"), ts)

    def ingest_assistant(self, msg, ts, fleet):
        self.last_stop = msg.get("stop_reason")
        if ts:  # calls from earlier messages have answered, or were interrupted and never will
            self.pending = {k: v for k, v in self.pending.items() if v >= ts - 2}
        content = msg.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                name = str(block.get("name") or "")
                inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                detail = detail_of(name, inp, self.base)
                self.action = {"tool": name, "detail": detail, "ts": ts}
                self.last_kind = "tool"
                if block.get("id"):
                    self.pending[block["id"]] = ts or time.time()
                fleet.activity.append((ts or 0.0, self, name, detail))
                if name == "SendMessage" and inp.get("message"):
                    self.sent.append({"ts": ts, "to": str(inp.get("to") or ""), "body": str(inp["message"]),
                                      "summary": str(inp.get("summary") or "")})
            elif btype == "text" and str(block.get("text") or "").strip():
                self.last_kind = "text"
                self.last_text_full = str(block["text"]).strip()
                self.last_text = first_line(self.last_text_full)
            elif btype in ("thinking", "redacted_thinking"):
                self.last_kind = "thinking"

    def ingest_user(self, rec, ts):
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self.pending.pop(block.get("tool_use_id"), None)
        self.scan_incoming(text_of(content), ts)

    def ingest_attachment(self, att, ts):
        if not isinstance(att, dict):
            return
        origin = att.get("origin")
        if isinstance(origin, dict) and origin.get("kind") == "peer" and origin.get("body"):
            self.add_incoming(origin.get("from") or "", origin.get("name") or "", str(origin["body"]), ts)
        if DOC_ATTACHMENT_RE.search(str(att.get("type") or "")):
            return
        for field in ("prompt", "content"):
            self.scan_incoming(att.get(field), ts)

    def scan_incoming(self, text, ts):
        if not isinstance(text, str) or "<cross-session-message" not in text:
            return
        for match in CROSS_RE.finditer(text):
            attrs = dict(ATTR_RE.findall(match.group(1)))
            self.add_incoming(attrs.get("from", ""), attrs.get("from-name", ""), match.group(2), ts)

    def add_incoming(self, addr, name, body, ts):
        addr, name, body = addr.strip(), name.strip(), body.strip()
        if body and (addr or name) and "..." not in (addr, name):
            self.received.append({"ts": ts, "from": name or addr, "body": body})


class Helper(Agent):
    """A subagent or workflow agent, listed under the session that started it."""

    def __init__(self, path, owner, team):
        self.owner = owner
        self.hid = os.path.basename(path)[len("agent-"): -len(".jsonl")]
        self.team = team
        self.meta = {}
        self.task = None          # (text, ts), read lazily from the head of the transcript
        super().__init__(path)
        try:
            with open(path[: -len(".jsonl")] + ".meta.json", encoding="utf-8") as fh:
                meta = json.load(fh)
            self.meta = meta if isinstance(meta, dict) else {}
        except (OSError, ValueError):
            pass

    @property
    def base(self):
        return self.owner.cwd

    @property
    def parent_id(self):
        return self.meta.get("parentAgentId")

    def get_task(self):
        if self.task is None:
            self.task = read_head(self.tail.path)
        return self.task

    def finished(self):
        journal = self.owner.journals.get(self.team) if self.team else None
        return bool(journal and self.hid in journal["done"]) or (self.last_stop == "end_turn" and not self.pending)

    def phase(self):
        journal = self.owner.journals.get(self.team) if self.team else None
        return self.meta.get("workflowPhase") or (journal or {}).get("agent_phase", {}).get(self.hid) or ""


class Session(Agent):
    """A Claude Code session: interactive (in a terminal) or background (`claude --bg`)."""

    backlog = BACKLOG_BYTES

    def __init__(self, sid, cwd):
        self.sid = sid
        self.cwd = cwd
        self.kind = "interactive"
        self.entry = {}           # from `claude agents --json`
        self.registry = {}        # from ~/.claude/sessions/<pid>.json
        self.pid = 0
        self.tty = ""
        self.helpers = {}         # transcript path -> Helper
        self.journals = {}        # wf id -> {"mtime", "agent_phase", "labels", "done", "phases"}
        self.scripts = {}         # wf id -> (name, phases)
        super().__init__(None)

    @property
    def base(self):
        return self.cwd

    @property
    def name(self):
        if self.kind == "background":
            return f"bg {self.entry.get('id') or self.sid[:8]}"
        return str(self.entry.get("name") or self.registry.get("name") or self.sid[:8])

    @property
    def title(self):
        if self.kind == "background":
            return str(self.entry.get("name") or self.custom_title or self.ai_title or "")
        return self.custom_title or self.ai_title or ""

    @property
    def status(self):
        if self.kind == "background":
            return str(self.entry.get("state") or self.entry.get("status") or "?")
        reg = str(self.registry.get("status") or "")
        status = str(self.entry.get("status") or reg or "?")
        return "shell" if reg == "shell" and status == "busy" else status  # busy, but only a background shell

    def by_hid(self):
        return {h.hid: h for h in self.helpers.values()}

    def scan_helpers(self, fleet, now):
        if not self.tail:
            return
        root = os.path.join(os.path.dirname(self.tail.path), self.sid)
        sub = os.path.join(root, "subagents")
        if not os.path.isdir(sub):
            return
        paths = [(p, None) for p in glob.glob(os.path.join(sub, "agent-*.jsonl"))]
        for wf_dir in glob.glob(os.path.join(sub, "workflows", "wf_*")):
            team = os.path.basename(wf_dir)
            paths += [(p, team) for p in glob.glob(os.path.join(wf_dir, "agent-*.jsonl"))]
            self.read_journal(team, os.path.join(wf_dir, "journal.jsonl"))
        for path, team in paths:
            helper = self.helpers.get(path)
            if helper is None:
                try:
                    if now - os.stat(path).st_mtime > HELPER_WINDOW:
                        continue
                except OSError:
                    continue
                helper = self.helpers[path] = Helper(path, self, team)
            helper.poll(fleet)
        for script in glob.glob(os.path.join(root, "workflows", "scripts", "*.js")):
            match = re.search(r"(wf_[\w-]+)\.js$", script)
            if match and match.group(1) not in self.scripts:
                self.scripts[match.group(1)] = parse_script_meta(script)

    def read_journal(self, team, path):
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            return
        known = self.journals.get(team)
        if known and known["mtime"] == mtime:
            return
        entry = {"mtime": mtime, "agent_phase": {}, "labels": {}, "done": set(), "phases": []}
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except OSError:
            return
        for raw in lines:
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            aid = rec.get("agentId") if isinstance(rec, dict) else None
            if not isinstance(aid, str):
                continue
            if rec.get("type") == "started":
                phase = str(rec.get("phase") or "")
                entry["agent_phase"][aid] = phase
                if rec.get("label"):
                    entry["labels"][aid] = str(rec["label"])
                if phase and phase not in entry["phases"]:
                    entry["phases"].append(phase)
            elif rec.get("type") in ("result", "error", "failed", "done", "completed"):
                entry["done"].add(aid)
        self.journals[team] = entry


def parse_script_meta(path):
    try:
        with open(path, encoding="utf-8") as fh:
            head = fh.read(6000)
    except OSError:
        return None, []
    meta = re.search(r"export\s+const\s+meta\s*=\s*\{(.*?)\n\}", head, re.S)
    block = meta.group(1) if meta else head
    name = WF_NAME_RE.search(block)
    return (name.group(1) if name else None), re.findall(r"""\btitle:\s*['"]([^'"]+)['"]""", block)


# ---------------------------------------------------------------- where sessions come from

class Feed:
    """`claude agents --json` polled in the background: Claude Code's own list of active sessions."""

    def __init__(self, claude_cmd, background=True):
        self.cmd = claude_cmd
        self.entries = None
        self.error = None
        if claude_cmd and background:
            threading.Thread(target=self.loop, daemon=True).start()

    def fetch(self):
        if not self.cmd:
            return None
        code, out, err = run(self.cmd + ["agents", "--json"], cwd=HOME, timeout=20)
        entries = json_in(out, list)
        if entries is None:
            self.error = first_line(err) or f"claude agents exited {code}"
            return None
        self.entries, self.error = [e for e in entries if isinstance(e, dict)], None
        return self.entries

    def loop(self):
        while True:
            self.fetch()
            time.sleep(FEED_EVERY)

    def status_now(self, sid):
        """A fresh status for one session, read right before typing into it."""
        for entry in self.fetch() or []:
            if entry.get("sessionId") == sid:
                return entry.get("status") or entry.get("state")
        return None


def read_registry():
    out = {}
    for path in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("sessionId") and isinstance(data.get("pid"), int):
            out[data["sessionId"]] = data
    return out


def process_table():
    """pid -> {ppid, tty, etime, command} for every process (one ps call)."""
    _code, out, _err = run(["ps", "-A", "-o", "pid=,ppid=,tty=,etime=,command="], timeout=10)
    table = {}
    for line in (out or "").splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        tty = parts[2] if parts[2] not in ("?", "??", "-") else ""
        table[int(parts[0])] = {"ppid": int(parts[1]), "tty": f"/dev/{tty}" if tty else "", "etime": parts[3],
                                "command": parts[4]}
    return table


class Repos:
    """Uncommitted and unpushed work in each session's repo, refreshed in the background (git is slow on big repos)."""

    EVERY = 30.0

    def __init__(self, background=True):
        self.folders = set()   # session folders to watch
        self.roots = {}        # folder -> repo root (or "")
        self.state = {}        # repo root -> {"dirty", "unpushed", "branch", "upstream"}
        if background:
            threading.Thread(target=self.loop, daemon=True).start()

    def watch(self, folders):
        self.folders = set(f for f in folders if f)

    def root(self, folder):
        if folder not in self.roots:
            code, out, _err = run(["git", "-C", folder, "rev-parse", "--show-toplevel"], timeout=10)
            self.roots[folder] = out.strip() if code == 0 else ""
        return self.roots[folder]

    def fetch(self):
        state = {}
        for folder in list(self.folders):
            root = self.root(folder)
            if not root or root in state:
                continue
            _c, status, _e = run(["git", "-C", root, "status", "--porcelain", "--branch"], timeout=20)
            lines = status.splitlines()
            head = lines[0] if lines and lines[0].startswith("## ") else ""
            code, ahead, _e = run(["git", "-C", root, "rev-list", "--count", "@{upstream}..HEAD"], timeout=10)
            upstream = code == 0
            if not upstream:  # no upstream: commits that are on no remote at all
                code, ahead, _e = run(["git", "-C", root, "rev-list", "--count", "HEAD", "--not", "--remotes"], timeout=10)
            state[root] = {"dirty": sum(1 for ln in lines if not ln.startswith("## ")),
                           "unpushed": int(ahead.strip()) if code == 0 and ahead.strip().isdigit() else 0,
                           "branch": head[3:].split("...")[0] if head else "", "upstream": upstream}
        self.state = state
        return state

    def loop(self):
        checked, at = None, 0.0
        while True:  # check at once when the set of folders changes (start-up, a new session), else every 30s
            folders = frozenset(self.folders)
            if folders != checked or time.time() - at >= self.EVERY:
                self.fetch()
                checked, at = folders, time.time()
            time.sleep(1)

    def of(self, folder):
        return self.state.get(self.roots.get(folder) or "", None)


class Procs:
    """The process table, refreshed in the background: `ps -A` takes ~1s on a busy Mac, too slow for the screen loop."""

    def __init__(self, background=True):
        self.snapshot = None  # (table, children, taken_at), replaced in one go
        if background:
            threading.Thread(target=self.loop, daemon=True).start()

    def fetch(self):
        table = process_table()
        children = {}
        for pid, proc in table.items():
            children.setdefault(proc["ppid"], []).append(pid)
        self.snapshot = (table, children, time.time())
        return self.snapshot

    def loop(self):
        while True:
            self.fetch()
            time.sleep(PROC_EVERY)

    def get(self):
        return self.snapshot or self.fetch()  # only the very first refresh waits for ps


def worker_processes(pid, table, children):
    """Agent CLIs (codex, kimi, claude -p ...) running under a session, skipping its MCP servers."""
    found, stack = [], list(children.get(pid, []))
    while stack:
        child = stack.pop()
        proc = table.get(child)
        if not proc:
            continue
        command = proc["command"]
        if "mcp" in command.lower():
            continue
        tokens = command.split()
        names = {os.path.basename(t) for t in tokens[:2]}
        if names & WORKER_NAMES:
            found.append({"pid": child, "etime": proc["etime"], "command": command})
        stack.extend(children.get(child, []))
    return found


def loose_agents(table, session_pids):
    """Agent CLIs running outside every session: route dispatches, codex or kimi in their own tab, claude -p."""
    out = []
    for pid, proc in table.items():
        command = proc["command"]
        tokens = command.split()
        names = {os.path.basename(t) for t in tokens[:2]}
        if not names & WORKER_NAMES or "mcp" in command.lower() or pid in session_pids:
            continue
        if "claude" in names and not ({"-p", "--print"} & set(tokens)):
            continue  # an interactive claude is a session (or about to be one), not a loose agent
        if " agents --json" in command:
            continue  # our own poll
        ancestor, hops = proc["ppid"], 0
        while ancestor > 1 and ancestor not in session_pids and hops < 50:
            parent = table.get(ancestor)
            if not parent:
                break
            if {os.path.basename(t) for t in parent["command"].split()[:2]} & WORKER_NAMES:
                break  # a child of another agent process: list the top one only
            ancestor, hops = parent["ppid"], hops + 1
        if ancestor in session_pids or (ancestor > 1 and table.get(ancestor) and
                                        {os.path.basename(t) for t in table[ancestor]["command"].split()[:2]} & WORKER_NAMES):
            continue
        tool = sorted(names & WORKER_NAMES)[0]
        age = etime_seconds(proc["etime"])
        parent = table.get(proc["ppid"], {}).get("command", "")
        out.append({"pid": pid, "tool": tool, "etime": proc["etime"], "age": age, "tty": proc["tty"], "command": command,
                    "parent": process_label(parent) if parent else "", "stale": age > STALE_AGENT_SECONDS})
    return sorted(out, key=lambda a: (a["parent"], a["tool"], -a["age"]))


def process_label(command):
    """A short name for a process: `node .../openclaw-2026.6.10/dist/index.js gateway` -> openclaw."""
    tokens = command.split()
    while tokens and (os.path.basename(tokens[0]) in LAUNCHERS or tokens[0].startswith("-")):
        tokens = tokens[1:]
    if not tokens:
        return ""
    parts = [p for p in tokens[0].split("/") if p and p not in PATH_NOISE]
    name = parts[-1] if parts else os.path.basename(tokens[0])
    return re.sub(r"-v?\d[\w.\-]*$", "", name)


# ---------------------------------------------------------------- herdr

class Herdr:
    """Herdr (herdr.dev), only while its server is already running: the agents in its panes (Claude, Codex,
    opencode, pi ...) and the way to steer them. atc never starts the server: starting it resumes every saved agent."""

    HERDR_GROUP = {"blocked": "blocked", "working": "working", "idle": "waiting", "done": "waiting"}

    def __init__(self, cmd, dry_run=False, background=True):
        self.cmd = cmd
        self.dry_run = dry_run
        self.running = False
        self.agents = []
        if cmd and background:
            threading.Thread(target=self.loop, daemon=True).start()

    def fetch(self):
        if not self.cmd:
            return
        code, out, _err = run(self.cmd + ["status", "server"], timeout=10)
        self.running = code == 0 and re.search(r"status:\s*running", out or "") is not None
        if not self.running:
            self.agents = []
            return
        _code, out, _err = run(self.cmd + ["agent", "list"], timeout=10)
        agents = ((json_in(out) or {}).get("result") or {}).get("agents")
        self.agents = [a for a in agents if isinstance(a, dict)] if isinstance(agents, list) else []

    def loop(self):
        while True:
            self.fetch()
            time.sleep(3)

    def by_session(self):
        """Claude session id -> the herdr agent running it."""
        out = {}
        for agent in self.agents:
            session = agent.get("agent_session") or {}
            if session.get("kind") == "id" and session.get("value"):
                out[str(session["value"])] = agent
        return out

    def act(self, pane, action, text=""):
        args = {"focus": ["agent", "focus", pane], "type": ["agent", "prompt", pane, text],
                "interrupt": ["agent", "send-keys", pane, "esc"]}[action]
        if self.dry_run:
            return "dry run: herdr " + " ".join(shlex.quote(a) for a in args)
        code, out, err = run(self.cmd + args, timeout=30)
        if code != 0:  # herdr refuses to prompt an agent at an approval dialog (agent_blocked)
            return f"herdr {action} failed: {first_line(err) or first_line(out) or code}"
        return f"{action}: done (herdr pane {pane})"


def herdr_view(agent, now):
    """A herdr agent that isn't a live Claude session we already list (Codex, opencode, pi ...)."""
    status = str(agent.get("agent_status") or "unknown")
    pane = str(agent.get("pane_id") or "?")
    name = f"{agent.get('agent') or 'agent'} {pane}"
    view = {
        "sid": f"herdr:{pane}", "kind": "herdr", "id": None, "name": name, "color": "c2",
        "cwd": str(agent.get("foreground_cwd") or agent.get("cwd") or ""), "pid": None, "tty": "", "version": None,
        "title": short_path(agent.get("cwd")), "status": status, "waiting_for": None, "since": None, "last": now,
        "started": None,
        "group": Herdr.HERDR_GROUP.get(status, "other"), "state": f"herdr {status}", "said": "",
        "doing": f"in herdr pane {pane}", "alert": "", "alert_style": "warn", "teams": [], "helpers": [],
        "helpers_hidden": 0, "helpers_working": 0, "workers": [], "herdr": pane,
    }
    if status == "blocked":
        view["doing"] = f"approval or question open in herdr pane {pane}"
    return view


# ---------------------------------------------------------------- other machines

def configured_hosts(extra=()):
    """Hosts from --host, ATC_HOSTS (comma separated) and ~/.config/atc/hosts (one per line, # comments)."""
    hosts = list(extra)
    hosts += [h.strip() for h in os.environ.get("ATC_HOSTS", "").split(",") if h.strip()]
    try:
        with open(HOSTS_FILE, encoding="utf-8") as fh:
            hosts += [line.split("#")[0].strip() for line in fh if line.split("#")[0].strip()]
    except OSError:
        pass
    return list(dict.fromkeys(hosts))


def unique_hosts(views):
    """Two ssh names for one machine (a NAS and its hostname) or this machine itself: show it once."""
    seen, out = {socket.gethostname().split(".")[0]: "this Mac"}, []
    for view in views:
        machine = str(view.get("machine") or "").split(".")[0]
        if machine and machine in seen:
            first = next((v for v in out if v["host"] == seen[machine]), None)
            if first is not None:
                first["aliases"].append(view["host"])
            continue
        if machine:
            seen[machine] = view["host"]
        out.append(dict(view, aliases=[]))
    return out


def feed_and_run(args, data, timeout):
    """Run args with `data` on stdin, writing and reading in threads, with a hard timeout.

    subprocess.run(input=...) can deadlock when the child writes before it reads and the pipe buffers are
    small (macOS hands out 512-byte pipe buffers under pressure), and its timeout never fires while a write
    blocks. Returns (exit code or None, stdout bytes, stderr bytes or an error string)."""
    try:
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        return None, b"", str(exc)
    got = {"out": b"", "err": b""}

    def read(name, stream):
        got[name] = stream.read()

    def write():
        try:
            proc.stdin.write(data)
        except OSError:
            pass  # the far end stopped reading, or never did
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    threads = [threading.Thread(target=read, args=("out", proc.stdout), daemon=True),
               threading.Thread(target=read, args=("err", proc.stderr), daemon=True),
               threading.Thread(target=write, daemon=True)]
    for t in threads:
        t.start()
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return None, b"", f"no answer within {timeout}s"
    for t in threads[:2]:
        t.join(timeout=5)
    return code, got["out"], got["err"]


class Remote:
    """atc on another machine, run over ssh from this very file: nothing has to be installed there."""

    def __init__(self, host, background=True):
        self.host = host
        self.data = None
        self.error = None
        self.ok_at = None
        self.latency = None
        if background:
            threading.Thread(target=self.loop, daemon=True).start()

    def fetch(self):
        try:
            with open(os.path.realpath(__file__), "rb") as fh:
                script = fh.read()
        except OSError as exc:
            self.error = str(exc)
            return
        start = time.time()
        ssh = shlex.split(os.environ.get("ATC_SSH") or "ssh")  # e.g. "ssh -J bastion"
        args = ssh + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=6", self.host, "python3", "-", "--json", "--no-hosts"]
        code, out, err = feed_and_run(args, script, timeout=45)
        if code is None:
            self.error = err or "ssh failed"
            return
        data = json_in(out.decode("utf-8", "replace"))
        if data is None:
            self.error = first_line(err.decode("utf-8", "replace") if isinstance(err, bytes) else err) or f"ssh exited {code}"
            return
        self.data, self.error, self.ok_at, self.latency = data, None, time.time(), time.time() - start

    def loop(self):
        while True:
            self.fetch()
            time.sleep(REMOTE_EVERY)

    def view(self, now):
        data = self.data or {}
        return {
            "host": self.host, "ok": self.error is None and self.data is not None, "error": self.error,
            "age": now - self.ok_at if self.ok_at else None, "latency": self.latency,
            "counts": data.get("counts", {}), "groups": data.get("groups", {}), "agents": data.get("agents", []),
            "machine": data.get("machine"),
        }


# ---------------------------------------------------------------- the fleet

class Fleet:
    def __init__(self, only=None, feed=None, herdr=None, procs=None, repos=None):
        self.only = only
        self.feed = feed
        self.herdr = herdr
        self.proc_watch = procs or Procs(background=False)
        self.repos = repos
        self.sessions = {}       # sid -> Session
        self.activity = []       # (ts, agent, tool, detail)
        self.addresses = {}      # messaging address -> session name
        self.procs = {}
        self.children = {}
        self.procs_at = 0.0
        self.missing_at = {}     # sid -> last time we looked for its transcript
        self.agents = []         # agent processes (codex, kimi, claude -p ...) outside every session

    def find_transcript(self, sid, cwd, now):
        slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
        path = os.path.join(PROJECTS_DIR, slug, f"{sid}.jsonl")
        if os.path.exists(path):
            return path
        if now - self.missing_at.get(sid, 0.0) < 10:
            return None
        self.missing_at[sid] = now
        found = glob.glob(os.path.join(PROJECTS_DIR, "*", f"{sid}.jsonl"))
        return found[0] if found else None

    def entries(self, registry):
        if self.feed and self.feed.entries is not None:
            return self.feed.entries
        return [  # no `claude agents`: fall back to the session registry, interactive sessions only
            {"sessionId": sid, "pid": d["pid"], "cwd": d.get("cwd"), "kind": "interactive", "name": d.get("name"),
             "status": d.get("status")}
            for sid, d in registry.items()
        ]

    def refresh(self, now):
        registry = read_registry()
        self.procs, self.children, self.procs_at = self.proc_watch.get()
        seen = set()
        for entry in self.entries(registry):
            sid = entry.get("sessionId")
            if not sid:
                continue
            cwd = str(entry.get("cwd") or "")
            if self.only and not within(cwd, self.only):
                continue
            kind = str(entry.get("kind") or "interactive")
            reg = registry.get(sid, {})
            pid = int(entry.get("pid") or reg.get("pid") or 0)
            if kind == "interactive":
                proc = self.procs.get(pid)
                if proc is None or "claude" not in proc["command"].lower():
                    continue  # exited, or the pid now belongs to something else
            seen.add(sid)
            session = self.sessions.get(sid)
            if session is None:
                session = self.sessions[sid] = Session(sid, cwd)
            session.kind, session.entry, session.registry, session.cwd, session.pid = kind, entry, reg, cwd, pid
            session.tty = self.procs.get(pid, {}).get("tty", "") if pid else ""
            if reg.get("messagingSocketPath"):
                self.addresses["uds:" + str(reg["messagingSocketPath"])] = session.name
            if session.tail is None:
                path = self.find_transcript(sid, cwd, now)
                if path:
                    session.attach(path)
            session.poll(self)
            session.scan_helpers(self, now)
        for sid in list(self.sessions):
            if sid not in seen:
                del self.sessions[sid]
        self.agents = loose_agents(self.procs, {s.pid for s in self.sessions.values() if s.pid}) if not self.only else []
        if self.repos is not None:
            self.repos.watch(s.cwd for s in self.sessions.values())
        order = sorted(self.sessions.values(), key=lambda s: parse_ts(s.entry.get("startedAt")) or 0)
        for i, session in enumerate(order):
            session.color = SESSION_COLORS[i % len(SESSION_COLORS)]
            session.label = session.name
        self.activity.sort(key=lambda item: item[0])
        del self.activity[:-ACTIVITY_KEEP]

    def resolve(self, address):
        address = REF_SUFFIX_RE.sub("", str(address or "").strip())
        if address in self.addresses:
            return self.addresses[address]
        match = re.match(r"uds:.*/(\d+)\.sock$", address)
        return f"closed session pid {match.group(1)}" if match else address

    def letters(self):
        """Messages between sessions, each counted once whether we saw it sent, received or both."""
        found = {}

        def add(frm, to, body, summary, ts):
            key = (frm, to, squash(body)[:100])
            cur = found.get(key)
            if cur is None:
                found[key] = {"kind": "letter", "from": frm, "to": to, "summary": summary or first_line(body),
                              "body": body, "ts": ts}
            else:
                if summary and cur["summary"] == first_line(cur["body"]):
                    cur["summary"] = summary
                if ts and (not cur["ts"] or ts < cur["ts"]):
                    cur["ts"] = ts

        for s in self.sessions.values():
            for m in list(s.sent) + [m for h in s.helpers.values() for m in h.sent]:
                to = self.resolve(m["to"])
                if to == "main" or AGENT_ID_RE.match(to):
                    continue  # a helper talking to its own session: see comms()
                add(s.name, to, m["body"], m["summary"], m["ts"])
            for m in s.received:
                add(self.resolve(m["from"]), s.name, m["body"], "", m["ts"])
        return sorted(found.values(), key=lambda x: x["ts"] or 0)

    def comms(self):
        """Every message: between sessions, a session's task to a helper, and the helper's report back."""
        items = self.letters()
        for s in self.sessions.values():
            helpers = s.by_hid()
            for h in s.helpers.values():
                parent = helpers.get(h.parent_id)
                boss = parent.label if parent else (f"{s.name} (workflow)" if h.team else s.name)
                text, ts = h.get_task()
                if text:
                    items.append({"kind": "task", "from": boss, "to": h.label, "summary": first_line(text),
                                  "body": text, "ts": ts or h.mtime})
                for m in h.sent:
                    to = self.resolve(m["to"])
                    if to == "main" or AGENT_ID_RE.match(to):
                        target = helpers.get(to)
                        items.append({"kind": "note", "from": h.label, "to": target.label if target else s.name,
                                      "summary": m["summary"] or first_line(m["body"]), "body": m["body"], "ts": m["ts"]})
                if h.finished() and h.last_text_full:
                    items.append({"kind": "report", "from": h.label, "to": boss, "summary": h.last_text,
                                  "body": h.last_text_full, "ts": h.last_ts or h.mtime})
        return sorted(items, key=lambda x: x["ts"] or 0)

    # -- the view model

    def model(self, now, quota=None, remotes=()):
        sessions = [session_view(s, now, self) for s in self.sessions.values()]
        for v in sessions:
            v["repo"] = self.repos.of(v["cwd"]) if self.repos is not None else None
        if self.herdr and self.herdr.running:
            panes = self.herdr.by_session()
            for v in sessions:
                agent = panes.pop(v["sid"], None)
                if agent:
                    v["herdr"] = str(agent.get("pane_id"))
                    if agent.get("agent_status") == "blocked" and v["group"] != "blocked":
                        v["group"], v["state"] = "blocked", "needs you"
            extra = [a for a in self.herdr.agents if a.get("pane_id") not in {v.get("herdr") for v in sessions}]
            for agent in extra:
                cwd = str(agent.get("cwd") or "")
                if not self.only or within(cwd, self.only):
                    sessions.append(herdr_view(agent, now))
        groups = {key: [] for key in ("blocked", "waiting", "working", "parked", "background", "other")}
        for v in sessions:
            groups[v["group"]].append(v)
        groups["blocked"].sort(key=lambda v: -(v["since"] or 0))
        groups["waiting"].sort(key=lambda v: v["since"] or 0)
        groups["working"].sort(key=lambda v: (v["started"] or 0, v["name"]))  # stable: rows must not jump under the cursor
        groups["parked"].sort(key=lambda v: v["since"] or 0)
        groups["background"].sort(key=lambda v: (v["started"] or 0, v["name"]))
        groups["other"].sort(key=lambda v: v["name"])
        helpers = [h for v in sessions for h in v["helpers"] + [a for t in v["teams"] for a in t["agents"]]]
        return {
            "now": now,
            "groups": groups,
            "counts": {
                "live": len(sessions),
                "blocked": len(groups["blocked"]),
                "waiting": len(groups["waiting"]),
                "working": len(groups["working"]),
                "parked": len(groups["parked"]),
                "background": len(groups["background"]),
                "other": len(groups["other"]),
                "helpers": len(helpers),
                "helpers_working": sum(1 for h in helpers if h["working"]),
                "agents": len(self.agents),
                "unsaved": len({(self.repos.roots.get(v["cwd"]) if self.repos else v["cwd"]) for v in sessions
                                if v.get("repo") and (v["repo"]["dirty"] or v["repo"]["unpushed"])}),
            },
            "agents": self.agents,
            "comms": self.comms(),
            "quota": quota,
            "machine": socket.gethostname(),
            "hosts": unique_hosts([r.view(now) for r in remotes]),
        }


def doing_text(agent):
    action = agent.action or {}
    if agent.last_kind == "tool" and action.get("tool"):
        return f"{short_tool(action['tool'])}  {action.get('detail') or ''}".strip()
    if agent.last_kind == "text":
        return "writing a reply"
    return "thinking"


def helper_label(h):
    label = ""
    for key in ("label", "description", "name"):
        if isinstance(h.meta.get(key), str) and h.meta[key].strip():
            label = h.meta[key]
            break
    journal = h.owner.journals.get(h.team) if h.team else None
    if not label and journal and h.hid in journal["labels"]:
        label = journal["labels"][h.hid]
    return squash(label) or f"helper {h.hid[:6]}"


def helper_view(h, now):
    finished = h.finished()
    recent = max(h.mtime, h.last_ts or 0.0)
    working = not finished and (now - recent < WORKING_SECONDS or bool(h.pending))
    h.label, h.color = helper_label(h), h.owner.color
    return {
        "id": h.hid, "label": h.label, "team": h.team, "finished": finished, "working": working, "last": recent,
        "doing": "done" if finished else doing_text(h), "phase": h.phase(), "parent": h.parent_id,
        "model": h.meta.get("model") or "", "worktree": bool(h.meta.get("worktreePath")),
    }


def team_view(team, info, script, members):
    name, script_phases = script or (None, [])
    phases = list(script_phases) + [p for p in (info or {}).get("phases", []) if p not in script_phases]
    current = None
    for row in sorted(members, key=lambda r: r["last"] or 0):
        current = row["phase"] or current
    if current and current not in phases:
        phases.append(current)
    in_current = [r for r in members if r["phase"] == current] or members
    current_done = all(r["finished"] for r in in_current)
    pos = phases.index(current) if current in phases else -1
    marks = []
    for i, phase in enumerate(phases):
        if i < pos or (i == pos and current_done):
            marks.append(("done", phase))
        elif i == pos:
            marks.append(("now", phase))
        else:
            marks.append(("todo", phase))
    return {
        "id": team, "name": name or team, "marks": marks, "phases": phases,
        "done": sum(1 for r in members if r["finished"]), "total": len(members),
        "agents": sorted(members, key=lambda r: r["label"]),
    }


def session_view(s, now, fleet):
    status = s.status
    since_ts = parse_ts(s.registry.get("statusUpdatedAt"))
    last = max(s.mtime, s.last_ts or 0.0)
    since = now - since_ts if since_ts else (now - last if last else None)

    rows = {h.hid: helper_view(h, now) for h in s.helpers.values()}
    helpers, teams_rows, hidden = [], {}, 0
    for h in s.helpers.values():
        row = rows[h.hid]
        visible = row["working"] or not row["finished"] or now - row["last"] < HELPER_DONE_SHOWN
        if h.team:
            teams_rows.setdefault(h.team, []).append((row, visible))
        elif visible:
            helpers.append(row)
        else:
            hidden += 1
    teams = []
    for team, team_rows in teams_rows.items():
        if not any(visible for _row, visible in team_rows):
            hidden += len(team_rows)
            continue
        teams.append(team_view(team, s.journals.get(team), s.scripts.get(team), [r for r, _v in team_rows]))
    helpers.sort(key=lambda r: r["label"])
    helpers_working = sum(1 for r in rows.values() if r["working"])
    workers = worker_processes(s.pid, fleet.procs, fleet.children) if s.pid else []

    alert, alert_style = "", "warn"
    if status == "busy":
        if s.pending:
            waited = now - max(s.pending.values())
            if waited > PENDING_WARN:
                alert = f"{short_tool((s.action or {}).get('tool') or 'tool')} running {human_age(waited)}"
        elif now - last > QUIET_WARN and not helpers_working and not workers:
            alert, alert_style = f"quiet for {human_age(now - last)}: stuck?", "err"

    age = human_age(since)
    if s.kind == "background":
        group, state = "background", f"{status} {human_age(now - last) if last else ''}".strip()
        said = f"“{s.last_text}”" if s.last_text else ""
        doing = doing_text(s) if status not in ("blocked", "done", "stopped") else said
    elif status == "waiting":
        group, state = "blocked", f"needs you {age}"
        need = s.entry.get("waitingFor") or "input needed"
        tool = (s.action or {}) if s.pending else {}
        doing = f"{need}: {short_tool(tool.get('tool'))}  {tool.get('detail') or ''}".strip() if tool else need
    elif status == "busy":
        group, state, doing = "working", f"busy {age}", doing_text(s)
    elif status == "idle" and (helpers_working or workers):
        group, state = "working", f"team {helpers_working + len(workers)}"
        doing = f"idle; {helpers_working} helper(s) and {len(workers)} worker process(es) running"
    elif status == "idle":
        group = "waiting" if (since or 0) < PARKED_AFTER else "parked"
        state = f"{'waiting' if group == 'waiting' else 'parked'} {age}"
        doing = f"“{s.last_text}”" if s.last_text else "waiting for you"
    elif now - last < WORKING_SECONDS * 2:  # e.g. "shell": the transcript says it's working
        group, state, doing = "working", f"{status} {age}", doing_text(s)
    else:
        group, state = "other", f"{status} {age}"
        doing = f"“{s.last_text}”" if s.last_text else ""

    return {
        "sid": s.sid, "kind": s.kind, "id": s.entry.get("id"), "name": s.name, "color": s.color, "cwd": s.cwd,
        "started": parse_ts(s.entry.get("startedAt")) or parse_ts(s.registry.get("startedAt")),
        "pid": s.pid or None, "tty": s.tty, "version": s.registry.get("version"), "title": s.title,
        "status": status, "waiting_for": s.entry.get("waitingFor"), "since": since, "last": last, "group": group,
        "state": state, "doing": doing, "said": s.last_text, "alert": alert, "alert_style": alert_style,
        "teams": teams, "helpers": helpers, "helpers_hidden": hidden, "helpers_working": helpers_working,
        "workers": workers,
    }


# ---------------------------------------------------------------- levers: quota, routing

class Quota:
    """quotamax's agent advice, polled in the background; it also pins the swarm cap."""

    def __init__(self, cmd, dry_run=False, background=True):
        self.cmd = cmd
        self.dry_run = dry_run
        self.data = None
        self.providers = []       # `quotamax providers --json`: codex, kimi, deepseek ...
        self.error = None
        self.wake = threading.Event()
        if cmd and background:
            threading.Thread(target=self.loop, daemon=True).start()

    def fetch(self):
        code, out, err = run(self.cmd + ["agent"], timeout=30)
        data = json_in(out)
        if data:
            self.data, self.error = data, None
        else:
            self.error = first_line(err) or f"quotamax exited {code}"
        providers = json_in(run(self.cmd + ["providers", "--json"], timeout=30)[1], list)
        if providers is not None:
            self.providers = [p for p in providers if isinstance(p, dict)]

    def loop(self):
        while True:
            self.fetch()
            self.wake.wait(QUOTA_EVERY)
            self.wake.clear()

    def level(self):
        data = self.data or {}
        override = data.get("override") or {}
        return override.get("level") or data.get("headroom")

    def step(self, delta):
        if not self.cmd:
            return "quotamax isn't installed, so there is no swarm cap to change"
        current = self.level()
        if current not in CAP_LEVELS:
            return "waiting for quotamax's first reading"
        target = CAP_LEVELS[max(0, min(len(CAP_LEVELS) - 1, CAP_LEVELS.index(current) + delta))]
        if target == current:
            return f"swarm cap is already at the {'top' if delta > 0 else 'bottom'}: {current}"
        hours = os.environ.get("ATC_OVERRIDE_HOURS", "2")
        return self.apply(["override", target, hours],
                          f"swarm cap pinned to {target} (up to {CAP_SIZE[target]} parallel agents) for {hours}h")

    def clear(self):
        if not self.cmd:
            return "quotamax isn't installed"
        return self.apply(["override", "clear"], "swarm cap back on auto: it follows measured quota again")

    def apply(self, args, message):
        if self.dry_run:
            return "dry run: " + " ".join(shlex.quote(a) for a in self.cmd + args)
        code, _out, err = run(self.cmd + args)
        self.wake.set()
        return message if code == 0 else f"quotamax failed: {first_line(err)}"


def route_plan(route_cmd, task, cwd):
    if not route_cmd:
        return None, "route isn't installed"
    code, out, err = run(route_cmd + ["task", "--plan-only", task], cwd=cwd, timeout=60)
    plan = json_in(out)
    if not plan:
        return None, first_line(err) or first_line(out) or f"route exited {code}"
    return plan, None


# ---------------------------------------------------------------- levers: terminals

def applescript_string(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def osascript(script):
    code, out, err = run(["osascript", "-e", script], timeout=20)
    return code == 0, (out.strip() or first_line(err))


def app_running(name):
    """Asks macOS, which unlike `tell application` never launches the app."""
    ok, result = osascript(f'application "{name}" is running')
    return ok and result == "true"


TTY_ACTIONS = {
    "iterm": {
        "find": "",
        "focus": "select w\n tell t to select\n tell s to select\n activate",
        "type": "tell s to write text {text} newline no\n delay 0.5\n tell s to write text \"\"",
        "interrupt": "tell s to write text (ASCII character 27) newline no",
    },
    "terminal": {
        "find": "",
        "focus": "set selected of t to true\n set index of w to 1\n activate",
        "type": "do script {text} in t",
    },
}


def tty_script(kind, tty, action, text=""):
    body = TTY_ACTIONS[kind][action].replace("{text}", applescript_string(text))
    if kind == "iterm":
        return f"""tell application "iTerm2"
 repeat with w in windows
  repeat with t in tabs of w
   repeat with s in sessions of t
    if tty of s is {applescript_string(tty)} then
     {body}
     return "found"
    end if
   end repeat
  end repeat
 end repeat
 return "missing"
end tell"""
    return f"""tell application "Terminal"
 repeat with w in windows
  repeat with t in tabs of w
   if tty of t is {applescript_string(tty)} then
    {body}
    return "found"
   end if
  end repeat
 end repeat
 return "missing"
end tell"""


class Terminals:
    """Finds which terminal owns a tty (tmux, iTerm2 or Terminal.app) and acts on that tab."""

    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.cache = {}  # tty -> (kind, target, at)
        self.blocked = None  # why macOS refused, when it did

    def locate(self, tty):
        now = time.time()
        hit = self.cache.get(tty)
        if hit and now - hit[2] < LOCATE_CACHE_SECONDS:
            return hit[0], hit[1]
        kind, target = None, None
        if shutil.which("tmux"):
            _code, out, _err = run(["tmux", "list-panes", "-a", "-F",
                                    "#{pane_tty} #{session_name}:#{window_index}.#{pane_index}"], timeout=5)
            for line in (out or "").splitlines():
                pane_tty, _, pane = line.partition(" ")
                if pane_tty == tty:
                    kind, target = "tmux", pane
        if kind is None and sys.platform == "darwin":
            for app, name in (("iterm", "iTerm2"), ("terminal", "Terminal")):
                if app_running(name):
                    ok, result = osascript(tty_script(app, tty, "find"))
                    if ok and result == "found":
                        kind = app
                        break
                    if not ok and ("-1743" in result or "Not authorized" in result or "-1744" in result):
                        self.blocked = (f"macOS hasn't let this app control {name}: allow it in System Settings › "
                                        f"Privacy & Security › Automation")
                        return None, None  # don't cache a refusal: it goes away once allowed
        self.cache[tty] = (kind, target, now)
        return kind, target

    def act(self, tty, action, text=""):
        """action: focus | type | interrupt. Returns a sentence saying what happened."""
        if not tty:
            return "that session has no terminal"
        self.blocked = None
        kind, target = self.locate(tty)
        if kind is None:
            return self.blocked or f"couldn't find the terminal tab on {tty} (supported: tmux, iTerm2, Terminal.app)"
        if self.dry_run:
            return f"dry run: would {action} the {kind} tab on {tty}" + (f": {text}" if text else "")
        if kind == "tmux":
            if action == "focus":
                run(["tmux", "switch-client", "-t", target.split(":")[0]])
                run(["tmux", "select-window", "-t", target.rsplit(".", 1)[0]])
                run(["tmux", "select-pane", "-t", target])
            elif action == "type":
                run(["tmux", "send-keys", "-t", target, "-l", text])
                time.sleep(0.5)  # let the text land before Enter, so it isn't swallowed as part of a paste
                run(["tmux", "send-keys", "-t", target, "Enter"])
            elif action == "interrupt":
                run(["tmux", "send-keys", "-t", target, "Escape"])
            return f"{action}: done (tmux pane {target})"
        if action not in TTY_ACTIONS[kind]:
            return f"{kind} can't {action}; try tmux or iTerm2"
        ok, result = osascript(tty_script(kind, tty, action, text))
        if not ok:
            return f"couldn't reach {kind}: {result}"
        if result != "found":
            self.cache.pop(tty, None)
            return f"the {kind} tab on {tty} has gone"
        return f"{action}: done ({kind} tab {tty})"


def terminal_kind():
    """Where new tabs open. ATC_TERMINAL: iterm, terminal, tmux, tmux-bg (windows in a detached `atc` tmux
    session you attach to from anywhere, ssh included), or none."""
    forced = os.environ.get("ATC_TERMINAL")
    if forced:
        return None if forced == "none" else forced
    if os.environ.get("TMUX"):
        return "tmux"
    kind = {"iTerm.app": "iterm", "Apple_Terminal": "terminal"}.get(os.environ.get("TERM_PROGRAM", ""))
    if kind or sys.platform != "darwin":
        return kind
    return "iterm" if app_running("iTerm2") else "terminal"  # run from an app (a menu bar, a launcher)


def open_tab(cwd, command, dry_run=False):
    """Run `command` in a new tab of the terminal atc is running in (or iTerm2 / Terminal.app when run from an app)."""
    line = f"cd {shlex.quote(cwd)} && {command}"
    kind = terminal_kind()
    if dry_run or kind is None:
        words = command.split(" ", 1)
        shown = " ".join([os.path.basename(words[0])] + words[1:])  # route, not /long/path/to/route
        return f"{'dry run' if dry_run else 'run this yourself'}: {shown}   (in {short_path(cwd)})"
    if kind == "tmux":
        code, _out, err = run(["tmux", "new-window", "-c", cwd, command])
        return "opened a tmux window" if code == 0 else f"tmux failed: {first_line(err)}"
    if kind == "tmux-bg":
        exists = run(["tmux", "has-session", "-t", "atc"])[0] == 0
        args = (["tmux", "new-window", "-t", "atc:", "-c", cwd, command] if exists
                else ["tmux", "new-session", "-d", "-s", "atc", "-c", cwd, command])
        code, _out, err = run(args)
        return ("started in tmux session atc (tmux attach -t atc)" if code == 0
                else f"tmux failed: {first_line(err)}")
    if kind == "iterm":
        script = f"""tell application "iTerm2"
 if (count of windows) is 0 then create window with default profile
 tell current window
  create tab with default profile
  tell current session to write text {applescript_string(line)}
 end tell
 activate
end tell"""
    else:
        script = f'tell application "Terminal"\n do script {applescript_string(line)}\n activate\nend tell'
    ok, result = osascript(script)
    return f"opened a new {kind} tab" if ok else f"couldn't open a tab: {result}"


# ---------------------------------------------------------------- rendering
# A frame is a list of lines; a line is a list of (text, style) segments. Curses and --once share it.

def clean(text):
    """One line of narrow characters only, so column maths holds (emoji are double width in a terminal)."""
    out = []
    for ch in str(text or ""):
        if ch in "\n\r\t":
            out.append(" ")
            continue
        code = ord(ch)
        if unicodedata.category(ch)[0] == "C" or 0xFE00 <= code <= 0xFE0F or code == 0x200D:
            continue
        if unicodedata.east_asian_width(ch) in ("W", "F") or code > 0xFFFF:
            continue
        if 0x2600 <= code <= 0x27BF or 0x2B00 <= code <= 0x2BFF:
            continue
        out.append(ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def seg_len(line):
    return sum(len(text) for text, _style in line)


def fit(line, width):
    if width <= 0:
        return []
    if seg_len(line) <= width:
        return line
    out, used = [], 0
    for text, style in line:
        room = width - used
        if room <= 0:
            break
        if len(text) > room:
            out.append((text[: max(0, room - 1)] + "…", style))
            return out
        out.append((text, style))
        used += len(text)
    return out


def pad(line, width):
    line = fit(line, width)
    gap = width - seg_len(line)
    return line + [(" " * gap, "plain")] if gap > 0 else line


def col(text, width):
    text = clean(text)
    if len(text) > width:
        text = text[: max(0, width - 1)] + "…"
    return f"{text:<{width}}"


def rule(title, width, extra=""):
    """A section heading across the width; the note on the right is dropped when it doesn't fit."""
    head = f"── {title} "
    tail = f" {extra} ──" if extra else ""
    if len(head) + len(tail) + 2 > width:
        tail = ""
    return fit([(head, "head"), ("─" * max(0, width - len(head) - len(tail)), "head"), (tail, "dim")], width)


def bar(done, total, width=10):
    if total <= 0:
        return "░" * width
    filled = round(width * done / total)
    return "█" * filled + "░" * (width - filled)


GROUP_LOOK = {
    "blocked": ("!", "err"), "waiting": ("◆", "warn"), "working": ("●", "busy"),
    "parked": ("·", "dim"), "background": ("◇", "c3"), "other": ("○", "dim"),
}


def phase_marks(team):
    line = []
    for i, (state, phase) in enumerate(team["marks"]):
        sym, style = {"done": ("✓", "ok"), "now": ("▸", "warn"), "todo": ("·", "dim")}[state]
        line += [(("  " if i else "") + sym + " ", style), (clean(phase), style)]
    return line + [("   " + bar(team["done"], team["total"]) + " ", "ok"), (f"{team['done']}/{team['total']} done", "plain")]


def session_lines(v, name_w, now):
    """The session's own row (tagged with its id, so it can be selected) and its helper rows."""
    dot, dot_style = GROUP_LOOK[v["group"]]
    state_style = {"blocked": "err", "waiting": "warn", "working": "busy", "background": "c3"}.get(v["group"], "dim")
    row = [(dot + " ", dot_style), (col(v["name"], name_w) + " ", v["color"]), (col(v["state"], 13) + " ", state_style)]
    if v["alert"]:
        row.append(("! " + clean(v["alert"]) + "   ", v["alert_style"]))
    row.append((clean(v["doing"]), "plain" if v["group"] in ("working", "waiting", "blocked") else "dim"))
    if v["title"] and v["kind"] != "background":
        row.append(("   " + clean(v["title"]), "dim"))
    elif v["title"]:
        row.append(("   " + clean(v["title"]), "dim"))
    repo = v.get("repo")
    if repo and (repo["dirty"] or repo["unpushed"]):
        row.append(("   " + unsaved_text(repo), "warn"))
    lines = [(row, v["sid"])]
    for team in v["teams"]:
        lines.append(([("     ⎿ ", "dim"), (clean(team["name"]) + "  ", v["color"])] + phase_marks(team), None))
        label_w = min(28, max(len(clean(a["label"])) for a in team["agents"]))
        for a in team["agents"]:
            lines.append((helper_line(a, now, "         ", label_w), f"{v['sid']}::{a['id']}"))
    if v["helpers"]:
        label_w = min(34, max(len(clean(a["label"])) for a in v["helpers"]))
        for a in v["helpers"]:
            lines.append((helper_line(a, now, "     ⎿ ", label_w), f"{v['sid']}::{a['id']}"))
    for w in v["workers"]:
        lines.append(([("     ⎿ ", "dim"), ("⚙ ", "c2"), (f"pid {w['pid']} up {human_age(etime_seconds(w['etime']))}  ", "dim"),
                       (clean(w["command"]), "plain")], None))
    return lines


def unsaved_text(repo):
    parts = []
    if repo["dirty"]:
        parts.append(f"{repo['dirty']} uncommitted")
    if repo["unpushed"]:
        parts.append(f"{repo['unpushed']} unpushed" + ("" if repo.get("upstream") else " (no upstream)"))
    return "⚠ " + " · ".join(parts)


def helper_dot(a):
    if a["finished"]:
        return "✓", "ok"
    if a["working"]:
        return "●", "busy"
    return "○", "dim"


def helper_line(a, now, indent, label_w):
    dot, style = helper_dot(a)
    return [
        (indent, "dim"), (dot + " ", style), (col(a["label"], label_w) + "  ", "plain"),
        (f"{human_age(now - a['last']) if a['last'] else '-':>4}  ", "dim"),
        (clean(a["doing"]), "plain" if a["working"] else "dim"),
    ]


def ordered_sessions(model, show_all):
    order = ["blocked", "waiting", "working"] + (["parked", "background", "other"] if show_all else [])
    return [v for key in order for v in model["groups"][key]]


def fleet_lines(model, width, show_all):
    """Every session row, grouped. Returns [(line, sid or None)]."""
    now, groups = model["now"], model["groups"]
    everyone = [v for g in groups.values() for v in g]
    name_w = min(18, max([len(clean(v["name"])) for v in everyone] + [8]))
    out = []
    titles = {
        "blocked": ("Blocked on you", "a prompt or question is open"),
        "waiting": ("Waiting on you", "newest first"),
        "working": ("Working", "oldest first"),
        "parked": ("Parked", "idle 12h+"),
        "background": ("Background", "claude --bg"),
        "other": ("Other", ""),
    }
    for key in ("blocked", "waiting", "working", "agents", "parked", "background", "other"):
        if key == "agents":
            if model.get("agents"):
                out.append((rule(f"Other agents ({len(model['agents'])})", width, "outside any Claude session"), None))
                for a in model["agents"]:
                    out.append((agent_row(a, name_w), f"@local::p::{a['pid']}"))
            continue
        if not groups[key]:
            continue
        title, extra = titles[key]
        if key in ("blocked", "waiting", "working") or show_all:
            out.append((rule(f"{title} ({len(groups[key])})", width, extra), None))
            for v in groups[key]:
                out += session_lines(v, name_w, now)
        else:
            names = ", ".join(clean(v["name"]) for v in groups[key])
            out.append(([(f"{title} ({len(groups[key])}): ", "dim"), (names, "dim"), ("   p lists them", "dim")], None))
    if not everyone:
        out.append(([("No live Claude Code sessions found.", "dim")], None))
    for h in model.get("hosts", []):
        out += host_lines(h, width, name_w)
    return out


def agent_row(a, name_w):
    line = [("⚙ ", "c2"), (col(a["tool"], name_w) + " ", "c2"),
            (col(f"up {human_age(a.get('age', etime_seconds(a['etime'])))}", 13) + " ", "warn" if a.get("stale") else "dim")]
    if a.get("stale"):
        line.append(("stale? ", "warn"))
    if a.get("parent"):
        line.append((f"under {a['parent']}  ", "plain"))
    return line + [(f"pid {a['pid']}  ", "dim"), (clean(a["command"]), "dim")]


def host_lines(h, width, name_w):
    """One remote machine: its Claude sessions (read-only here) and its agent processes."""
    if not h["ok"]:
        return [(rule(h["host"], width, "unreachable"), None), ([("  " + clean(h["error"] or "no answer"), "err")], None)]
    when = f"{h['latency']:.1f}s ssh · {human_age(h['age'])} ago" if h["age"] is not None else ""
    name = h["host"] + (f" (also {', '.join(h['aliases'])})" if h.get("aliases") else "")
    out = [(rule(name, width, when), None)]
    remote_sessions = [v for key in ("blocked", "waiting", "working") for v in h["groups"].get(key, [])]
    for v in remote_sessions:
        dot, dot_style = GROUP_LOOK.get(v.get("group"), ("○", "dim"))
        out.append(([(dot + " ", dot_style), (col(v.get("name"), name_w) + " ", "c3"),
                     (col(v.get("state"), 13) + " ", "plain"), (clean(v.get("doing")), "plain")],
                    f"@{h['host']}::s::{v.get('sid')}"))
    quiet = sum(len(h["groups"].get(k, [])) for k in ("parked", "background", "other"))
    if quiet:
        out.append(([(f"  + {quiet} idle Claude sessions", "dim")], None))
    for a in h["agents"]:
        out.append((agent_row(a, name_w), f"@{h['host']}::p::{a['pid']}"))
    if not remote_sessions and not quiet and not h["agents"]:
        out.append(([("  no Claude sessions or agent processes", "dim")], None))
    return out


def activity_lines(items, limit, scroll=0):
    items = items[::-1][scroll: scroll + limit]
    label_w = min(22, max([len(clean(a.label)) for _ts, a, _t, _d in items] + [6]))
    lines = []
    for ts, agent, tool, detail in items:
        when = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "--:--:--"
        lines.append([
            (when + "  ", "dim"), (col(agent.label, label_w) + "  ", agent.color),
            (col(short_tool(tool), 16) + " ", "warn" if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit") else "plain"),
            (clean(detail), "dim"),
        ])
    return lines or [[("Nothing yet.", "dim")]]


KIND_LOOK = {"letter": ("✉", "c1"), "task": ("→", "c0"), "report": ("←", "ok"), "note": ("·", "dim")}


def comm_line(item):
    when = datetime.fromtimestamp(item["ts"]).strftime("%H:%M") if item.get("ts") else "--:--"
    sym, style = KIND_LOOK.get(item["kind"], ("?", "dim"))
    return [
        (when + " ", "dim"), (sym + " ", style), (clean(item["from"])[:24], style), (" → ", "dim"),
        (clean(item["to"])[:24], style), ("  " + clean(item.get("summary")), "plain"),
    ]


def comm_lines(items, limit):
    return [comm_line(item) for item in reversed(items[-limit:])] or [[("No messages yet.", "dim")]]


def resolve(fleet, model, key):
    """What a selected row is: ("session", v) | ("helper", v, row, helper) | ("process", host, agent) |
    ("remote", host, v) | (None,)."""
    if not key:
        return (None,)
    if key.startswith("@"):
        parts = key[1:].split("::", 2)
        if len(parts) != 3:
            return (None,)
        host, kind, ident = parts
        if host == "local":
            agent = next((a for a in model.get("agents", []) if str(a["pid"]) == ident), None)
            return ("process", None, agent) if agent else (None,)
        h = next((x for x in model.get("hosts", []) if x["host"] == host), None)
        if h is None:
            return (None,)
        if kind == "p":
            agent = next((a for a in h["agents"] if str(a["pid"]) == ident), None)
            return ("process", h, agent) if agent else (None,)
        v = next((x for g in h["groups"].values() for x in g if str(x.get("sid")) == ident), None)
        return ("remote", h, v) if v else (None,)
    sid, _, hid = key.partition("::")
    v = next((x for x in ordered_sessions(model, True) if x["sid"] == sid), None)
    if v is None:
        return (None,)
    if not hid:
        return ("session", v)
    session = fleet.sessions.get(sid)
    helper = next((h for h in session.helpers.values() if h.hid == hid), None) if session else None
    row = next((a for a in v["helpers"] + [a for t in v["teams"] for a in t["agents"]] if a["id"] == hid), None)
    return ("helper", v, row, helper) if helper and row else ("session", v)


def selection_name(item):
    kind = item[0]
    if kind == "session":
        return item[1]["name"]
    if kind == "helper":
        return f"{item[2]['label']} (in {item[1]['name']})"
    if kind == "process":
        return f"{item[2]['tool']} pid {item[2]['pid']}" + (f" on {item[1]['host']}" if item[1] else "")
    if kind == "remote":
        return f"{item[2].get('name')} on {item[1]['host']}"
    return "-"


def selection_lines(fleet, model, item, limit, width):
    """The bottom panel, or the full view, for whatever row is selected."""
    kind = item[0]
    if kind == "session":
        return detail_lines(fleet, item[1], limit)
    if kind == "helper":
        return helper_lines(fleet, item[1], item[2], item[3], limit, width)
    if kind == "process":
        host, a = item[1], item[2]
        lines = [[("process  ", "dim"), (f"{a['tool']}  pid {a['pid']}", "c2"),
                  (f"   on {host['host'] if host else 'this Mac'}", "plain"),
                  (f"   up {human_age(a.get('age', etime_seconds(a['etime'])))}", "warn" if a.get("stale") else "dim")]]
        if a.get("parent"):
            lines.append([("started by ", "dim"), (clean(a["parent"]), "plain")])
        if a.get("stale"):
            lines.append([("stale?  ", "warn"), ("running for over a day: probably stuck or never cleaned up", "warn")])
        lines.append([("command ", "dim")])
        lines += [[("  " + part, "plain")] for part in wrap(a["command"], width - 4)]
        lines.append([("x x stops it (SIGTERM" + (" over ssh)" if host else ")"), "dim")])
        return lines[:limit] if limit else lines
    if kind == "remote":
        h, v = item[1], item[2]
        lines = [[(clean(v.get("name")) + "  ", "c3"), (f"on {h['host']}  ", "plain"), (short_path(v.get("cwd")), "dim")],
                 [("state  ", "dim"), (clean(v.get("state")) + "   ", "plain"), (clean(v.get("doing")), "plain")],
                 [("title  ", "dim"), (clean(v.get("title")) or "-", "plain")],
                 [("enter opens an ssh shell there in a new tab", "dim")]]
        return lines
    return [[("Select a row with up/down.", "dim")]]


def helper_lines(fleet, v, row, helper, limit, width):
    """A subagent or workflow agent: who started it, its task, its steps and its report."""
    session = fleet.sessions.get(v["sid"])
    parent = session.by_hid().get(row["parent"]) if session and row["parent"] else None
    dot, style = helper_dot(row)
    tags = "  ".join(t for t in (row["model"], f"phase {row['phase']}" if row["phase"] else "",
                                  "worktree" if row["worktree"] else "") if t)
    lines = [[(dot + " ", style), (clean(row["label"]) + "  ", v["color"]), (tags, "dim")],
             [("started by ", "dim"), (clean(parent.label if parent else v["name"]), "plain"),
              ("   " + ("done" if row["finished"] else clean(row["doing"])), "plain" if row["working"] else "dim")]]
    text, _ts = helper.get_task()
    if text:
        lines.append([("task", "head")])
        task = wrap(text, width - 4)
        lines += [[("  " + part, "plain")] for part in task[:8]]
        if len(task) > 8:
            lines.append([(f"  … {len(task) - 8} more lines (c shows the whole message)", "dim")])
    if row["finished"] and helper.last_text_full:
        lines.append([("report", "head")])
        lines += [[("  " + part, "ok")] for part in wrap(helper.last_text_full, width - 4)[:8]]
    kids = [h for h in (session.helpers.values() if session else []) if h.parent_id == helper.hid]
    if kids:
        lines.append([("its own helpers: ", "dim"), (", ".join(clean(k.label) for k in kids), "plain")])
    steps = [item for item in fleet.activity if item[1] is helper]
    lines.append([("its steps, newest first", "head")])
    lines += activity_lines(steps, max(1, (limit or 200) - len(lines)))
    return lines[:limit] if limit else lines


def stop_process(host, pid, dry_run=False):
    """SIGTERM an agent process here, or on a server over ssh."""
    where = f" on {host}" if host else ""
    if dry_run:
        return f"dry run: would stop pid {pid}{where}"
    if not host:
        try:
            os.kill(int(pid), signal.SIGTERM)
            return f"stopped pid {pid}"
        except ProcessLookupError:
            return f"pid {pid} had already gone"
        except PermissionError:
            return f"not allowed to stop pid {pid}: it belongs to another user"
    ssh = shlex.split(os.environ.get("ATC_SSH") or "ssh")
    code, _out, err = run(ssh + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=6", host, "kill", str(int(pid))],
                          timeout=20)
    return f"stopped pid {pid}{where}" if code == 0 else f"couldn't stop pid {pid}{where}: {first_line(err) or code}"


def detail_lines(fleet, v, limit):
    if not v:
        return [[("Select a session with up/down.", "dim")]]
    lines = [
        [(clean(v["name"]) + "  ", v["color"]), (short_path(v["cwd"]), "plain"),
         (f"   {v['kind']}  pid {v['pid'] or '-'}  {v['tty'] or 'no tty'}  claude {v['version'] or '?'}", "dim")],
        [("title  ", "dim"), (clean(v["title"]) or "-", "plain")],
        [("state  ", "dim"), (clean(v["state"]) + "   ", "plain"), (clean(v["doing"]), "plain")],
    ]
    if v["said"]:
        lines.append([("said   ", "dim"), (clean(v["said"]), "plain")])
    if v["alert"]:
        lines.append([("alert  ", "dim"), (clean(v["alert"]), v["alert_style"])])
    if v.get("repo"):
        repo = v["repo"]
        state = unsaved_text(repo) if repo["dirty"] or repo["unpushed"] else "everything committed and pushed"
        lines.append([("git    ", "dim"), (f"{repo['branch'] or '?'}  ", "plain"),
                      (state, "warn" if repo["dirty"] or repo["unpushed"] else "ok")])
    session = fleet.sessions.get(v["sid"])
    mine = [item for item in fleet.activity if item[1] is session or getattr(item[1], "owner", None) is session]
    lines.append(rule("its activity", 40))
    return lines + activity_lines(mine, max(1, limit - len(lines)))


def header_line(model):
    c, q = model["counts"], model["quota"]
    line = [
        (" atc ", "title"), (f" {datetime.fromtimestamp(model['now']).strftime('%H:%M:%S')}  ", "dim"),
        (f"{c['live']} sessions · ", "plain"),
    ]
    if c["blocked"]:
        line.append((f"{c['blocked']} blocked · ", "err"))
    line += [
        (f"{c['waiting']} waiting", "warn" if c["waiting"] else "dim"), (" · ", "dim"),
        (f"{c['working']} working", "busy" if c["working"] else "dim"),
        (f" · helpers {c['helpers_working']}/{c['helpers']}", "plain"),
    ]
    if c["agents"]:
        line.append((f" · other agents {c['agents']}", "c2"))
    if c.get("unsaved"):
        line.append((f" · ⚠ unsaved work in {c['unsaved']} repo{'s' if c['unsaved'] != 1 else ''}", "warn"))
    for h in model.get("hosts", []):
        if not h["ok"]:
            line.append((f" · {h['host']} unreachable", "err"))
            continue
        hc = h["counts"]
        busy = hc.get("working", 0) + hc.get("blocked", 0) + hc.get("waiting", 0)
        stale = sum(1 for a in h["agents"] if a.get("stale"))
        line.append((f" · {h['host']} {busy} sessions {len(h['agents'])} agents", "c3"))
        if stale:
            line.append((f" ({stale} stale)", "warn"))
    return line


def quota_line(q):
    """Claude, then every other provider quotamax knows, then the swarm cap."""
    if not q or not q.cmd:
        return [("quota  ", "dim"), ("install quotamax for quota and the swarm cap", "dim")]
    data = q.data
    if not data:
        return [("quota  ", "dim"), (q.error or "…", "dim")]
    session = (data.get("session") or {}).get("percentUsed")
    weekly = (data.get("weekly") or {}).get("effectivePercent", (data.get("weekly") or {}).get("percentUsed"))
    level = q.level()
    override = data.get("override") or {}
    size = CAP_SIZE.get(level, (data.get("advice") or {}).get("parallelism", "?"))
    style = {"abundant": "ok", "comfortable": "ok", "constrained": "warn", "critical": "err"}.get(level, "plain")
    pinned = "pinned" if override.get("level") else "auto"
    until = parse_ts(override.get("until")) if override.get("until") else None
    if until:
        pinned += f" to {datetime.fromtimestamp(until).strftime('%H:%M')}"
    line = [("quota  ", "dim"), ("Claude ", "plain"), (f"5h {session}% · week {weekly}%", pct_style(weekly))]
    for p in q.providers:
        if not p.get("ok") or not p.get("configured", True) or p.get("id") == "claude":
            continue
        name = clean(str(p.get("label") or p.get("id"))).split(" ")[0]
        parts = [(f"{l.get('label', '').replace(' window', '')} {l.get('percent')}%", pct_style(l.get("percent")))
                 for l in p.get("limits") or [] if l.get("percent") is not None]
        if p.get("balanceUsd") is not None:
            parts.append((f"${p['balanceUsd']:.2f}", "err" if p.get("low") else "plain"))
        if parts:
            line.append((f"   {name} ", "plain"))
            for i, part in enumerate(parts):
                line += ([(" · ", "dim")] if i else []) + [part]
    return line + [("   │ swarm cap ", "dim"), (f"≤{size} {level}", style), (f" ({pinned})", "dim")]


def pct_style(percent):
    try:
        percent = float(percent)
    except (TypeError, ValueError):
        return "plain"
    return "err" if percent >= 90 else ("warn" if percent >= 75 else "plain")


# ---------------------------------------------------------------- the hierarchy

def tree_lines(fleet, model, only_sid=None):
    """Session > workflow > phase > agent > nested agent, plus worker processes and who it talks to."""
    now = model["now"]
    letters = [c for c in model["comms"] if c["kind"] == "letter"]
    out = []
    for v in ordered_sessions(model, True):
        if only_sid and v["sid"] != only_sid:
            continue
        s = fleet.sessions.get(v["sid"])
        if s is None:
            continue
        dot, dot_style = GROUP_LOOK[v["group"]]
        out.append([(dot + " ", dot_style), (clean(v["name"]) + "  ", v["color"]), (clean(v["state"]) + "  ", "plain"),
                    (clean(v["title"]) + "  ", "dim"), (short_path(v["cwd"]), "dim")])
        out += draw_tree(session_nodes(s, v, now, letters))
        out.append([])
    if not only_sid:
        for h in model.get("hosts", []):
            out += host_tree(h)
    return out or [[("No sessions.", "dim")]]


def host_tree(h):
    """A remote machine: its Claude sessions, and its agent processes grouped by what started them."""
    if not h["ok"]:
        return [[("■ ", "err"), (h["host"] + "  ", "c3"), (clean(h["error"] or "unreachable"), "err")], []]
    nodes = []
    for key in ("blocked", "waiting", "working", "parked", "background", "other"):
        for v in h["groups"].get(key, []):
            nodes.append({"line": [(clean(v.get("name")) + "  ", "c3"), (clean(v.get("state")) + "  ", "plain"),
                                   (clean(v.get("title")), "dim")], "children": []})
    by_parent = {}
    for a in h["agents"]:
        by_parent.setdefault(a.get("parent") or "?", []).append(a)
    for parent, agents in sorted(by_parent.items()):
        stale = sum(1 for a in agents if a.get("stale"))
        kids = [{"line": [("⚙ ", "c2"), (f"{a['tool']}  up {human_age(a.get('age', 0))}  ", "warn" if a.get("stale") else "plain"),
                          (f"pid {a['pid']}", "dim")], "children": []} for a in agents]
        tools = ", ".join(sorted({a["tool"] for a in agents}))
        nodes.append({"line": [(f"{parent}  ", "plain"), (f"{len(agents)} {tools}", "c2")]
                      + ([(f"  {stale} running over a day", "warn")] if stale else []), "children": kids})
    return [[("■ ", "c3"), (h["host"], "c3"), (f"  {h['latency']:.1f}s over ssh", "dim")]] + draw_tree(nodes) + [[]]


def agent_node(row, kids):
    dot, style = helper_dot(row)
    tags = " ".join(t for t in (row["model"], "worktree" if row["worktree"] else "") if t)
    line = [(dot + " ", style), (clean(row["label"]) + "  ", "plain")]
    if tags:
        line.append((f"[{tags}]  ", "dim"))
    line.append((clean(row["doing"]), "plain" if row["working"] else "dim"))
    return {"line": line, "children": kids}


def session_nodes(s, v, now, letters):
    rows = {h.hid: helper_view(h, now) for h in s.helpers.values()}
    kids = {}
    for hid, row in rows.items():
        if row["parent"] in rows:
            kids.setdefault(row["parent"], []).append(hid)

    def build(hid):
        return agent_node(rows[hid], [build(k) for k in sorted(kids.get(hid, []), key=lambda k: rows[k]["label"])])

    roots = [hid for hid, row in rows.items() if row["parent"] not in rows]
    nodes, by_team = [], {}
    for hid in roots:
        team = rows[hid]["team"]
        if team:
            by_team.setdefault(team, []).append(hid)
    for team, hids in by_team.items():
        info = team_view(team, s.journals.get(team), s.scripts.get(team), [rows[h] for h in hids])
        phase_nodes = []
        for phase in info["phases"] or [""]:
            members = sorted((h for h in hids if rows[h]["phase"] == phase), key=lambda h: rows[h]["label"])
            if not members:
                continue
            done = sum(1 for h in members if rows[h]["finished"])
            phase_nodes.append({"line": [("phase ", "dim"), (clean(phase) or "-", "warn"),
                                         (f"  {done}/{len(members)} done", "dim")],
                                "children": [build(h) for h in members]})
        stray = [h for h in hids if rows[h]["phase"] not in (info["phases"] or [""])]
        phase_nodes += [build(h) for h in stray]
        nodes.append({"line": [("workflow ", "dim"), (clean(info["name"]) + "  ", v["color"])] + phase_marks(info),
                      "children": phase_nodes})
    for hid in sorted((h for h in roots if not rows[h]["team"]), key=lambda h: rows[h]["label"]):
        nodes.append(build(hid))
    for w in v["workers"]:
        nodes.append({"line": [("process ", "dim"), (clean(w["command"])[:90], "c2"),
                               (f"  pid {w['pid']} up {human_age(etime_seconds(w['etime']))}", "dim")], "children": []})
    counts = {}
    for item in letters:
        if item["from"] == v["name"]:
            counts[item["to"]] = counts.get(item["to"], 0) + 1
        elif item["to"] == v["name"]:
            counts[item["from"]] = counts.get(item["from"], 0) + 1
    if counts:
        talk = ", ".join(f"{clean(k)} ×{n}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))
        nodes.append({"line": [("talks with ", "dim"), (talk, "c1")], "children": []})
    return nodes


def draw_tree(nodes, prefix=""):
    out = []
    for i, node in enumerate(nodes):
        last = i == len(nodes) - 1
        out.append([(prefix + ("└─ " if last else "├─ "), "dim")] + node["line"])
        out += draw_tree(node["children"], prefix + ("   " if last else "│  "))
    return out


# ---------------------------------------------------------------- the screen

KEYS = ("↑↓ select · enter jump/open · m msg · M broadcast · x x interrupt/stop · C close · F fork · c comms · "
        "h tree · n task · +/- cap · tab · p · ? help · q quit")

HELP = [
    "enter      jump to the session's terminal tab; a background session opens attached in a new tab",
    "m          type a message into the selected session, as if you typed it in its tab",
    "M          type the same message into every session in the selected session's project (its folder)",
    "x x        interrupt the selected session (sends Esc); a background session: `claude stop`;",
    "           an agent process (here or on a server): SIGTERM, over ssh for a server",
    "enter      on a subagent, a process or a server row: open it (its task, steps, report, command)",
    "C C / F F  type /closecode or /forkcode into the selected session (wrap it up, or split work off)",
    "           Steering never types into a session that is blocked on a prompt: that would answer it.",
    "c          comms: every message: session to session (✉), task to a helper (→), report back (←)",
    "h          the hierarchy: session > workflow > phase > agent > nested agent, worker processes",
    "           in comms and the tree, s switches between all sessions and the selected one",
    "n          new task: route plans where it should run; enter dispatches it in a new tab",
    "+ - 0      swarm cap via quotamax: raise, lower (pinned for ATC_OVERRIDE_HOURS, default 2h), auto",
    "tab        bottom panel: all activity, the selected session, comms",
    "p          hide or show parked, background and other sessions (shown by default)",
    "PgUp PgDn  scroll the bottom panel or the open view",
    "q          quit (esc closes a view or cancels input)",
]


def render(fleet, model, ui, width, height=None):
    """The main screen. Returns (frame, selectable session ids)."""
    ui["width"] = width
    frame = [fit(header_line(model), width), fit(quota_line(model["quota"]), width)]
    rows = fleet_lines(model, width, ui["show_all"] or height is None)
    selectable = [sid for _l, sid in rows if sid]
    item = resolve(fleet, model, ui.get("selected"))

    if height is None:  # --once: everything, no scrolling
        frame += [fit(line, width) for line, _sid in rows]
        frame.append(rule("Activity", width, "newest first"))
        frame += [fit(line, width) for line in activity_lines(fleet.activity, 15)]
        frame.append(rule("Comms", width, "✉ between sessions · → task to a helper · ← report back"))
        frame += [fit(line, width) for line in comm_lines(model["comms"], 12)]
        return frame, selectable

    body = height - 3  # header, quota line and footer
    top_h = max(4, min(len(rows), int(body * 0.6)))
    bottom_h = max(3, body - top_h)
    sel_index = next((i for i, (_l, sid) in enumerate(rows) if sid and sid == ui["selected"]), 0)
    top = max(0, min(ui.get("top_scroll", 0), sel_index), sel_index - top_h + 1)
    ui["top_scroll"] = top = min(top, max(0, len(rows) - top_h))
    shown = rows[top: top + top_h]
    for line, sid in shown:
        if line and line[0][1] == "head":  # a section heading: full width, no selection gutter
            frame.append(fit(line, width))
            continue
        line = fit(line, width - 2)
        if sid and sid == ui["selected"]:
            frame.append([("▶ ", "sel")] + [(t, s + "+sel") for t, s in pad(line, width - 2)])
        else:
            frame.append([("  ", "plain")] + line)
    frame += [[]] * (top_h - len(shown))

    panel = ui["panel"]
    room = bottom_h - 1
    if panel == "session":
        title, left = f"Selected: {selection_name(item)}", selection_lines(fleet, model, item, room, width)
    elif panel == "comms" and width < 150:
        title, left = "Comms (c opens the full view)", comm_lines(model["comms"], room)
    else:
        title, left = "Activity, all sessions", activity_lines(fleet.activity, room, ui["scroll"])
    if width >= 150:
        left_w = width * 3 // 5
        right_w = width - left_w - 2
        frame.append(rule(title, left_w) + [("  ", "plain")] + rule("Comms", right_w, "c opens"))
        right = comm_lines(model["comms"], room)
        for i in range(room):
            frame.append(pad(left[i] if i < len(left) else [], left_w) + [("  ", "plain")]
                         + fit(right[i] if i < len(right) else [], right_w))
    else:
        frame.append(rule(title, width, "tab switches"))
        frame += [fit(line, width) for line in left[:room]]
    frame = frame[: height - 1]
    frame += [[]] * (height - 1 - len(frame))
    frame.append(fit(ui["footer"], width))
    return frame, selectable


def wrap(text, width):
    lines = []
    for para in str(text or "").splitlines() or [""]:
        lines += textwrap.wrap(clean(para), max(10, width)) or [""]
    return lines


def render_view(fleet, model, ui, width, height):
    """Full-screen views: comms, tree, help."""
    view = ui["view"]
    selected = ui["selected"]
    mine = ui["view_mine"] and selected
    key = selected  # the full row key: the detail view needs it whole
    selected = (selected or "").split("::")[0] or None  # comms and tree filter by the session
    name = next((v["name"] for v in ordered_sessions(model, True) if v["sid"] == selected), None)
    if mine and name is None:
        mine = False  # a process or a server row: show everything
    frame = [fit(header_line(model), width), fit(quota_line(model["quota"]), width)]
    room = height - 4
    if view == "help":
        frame.append(rule("Keys", width))
        frame += [[(line, "plain")] for line in HELP]
    elif view == "detail":
        item = resolve(fleet, model, key)
        lines = selection_lines(fleet, model, item, 0, width)
        ui["view_scroll"] = max(0, min(ui["view_scroll"], max(0, len(lines) - room)))
        frame.append(rule(selection_name(item), width, "↑↓ PgUp PgDn scroll · esc closes"))
        frame += [fit(line, width) for line in lines[ui["view_scroll"]: ui["view_scroll"] + room]]
    elif view == "tree":
        lines = tree_lines(fleet, model, selected if mine else None)
        ui["view_scroll"] = max(0, min(ui["view_scroll"], max(0, len(lines) - room)))
        frame.append(rule(f"Hierarchy: {name if mine else 'all sessions'}", width,
                          "s: all / selected · ↑↓ PgUp PgDn scroll · esc closes"))
        frame += [fit(line, width) for line in lines[ui["view_scroll"]: ui["view_scroll"] + room]]
    else:
        items = list(reversed(model["comms"]))
        if mine:
            helpers = {h.label for h in fleet.sessions[selected].helpers.values()} if selected in fleet.sessions else set()
            names = {name, f"{name} (workflow)"} | helpers
            items = [c for c in items if c["from"] in names or c["to"] in names]
        ui["view_pick"] = max(0, min(ui["view_pick"], len(items) - 1))
        list_h = max(3, room // 2)
        top = max(0, ui["view_pick"] - list_h + 1)
        frame.append(rule(f"Comms: {name if mine else 'all sessions'} ({len(items)})", width,
                          "✉ letter → task ← report · s: all / selected · ↑↓ pick · esc closes"))
        for i, item in enumerate(items[top: top + list_h], start=top):
            line = fit(comm_line(item), width - 2)
            if i == ui["view_pick"]:
                frame.append([("▶ ", "sel")] + [(t, s + "+sel") for t, s in pad(line, width - 2)])
            else:
                frame.append([("  ", "plain")] + line)
        frame += [[]] * (list_h - min(list_h, len(items) - top))
        if items:
            item = items[ui["view_pick"]]
            when = datetime.fromtimestamp(item["ts"]).strftime("%a %H:%M:%S") if item.get("ts") else ""
            frame.append(rule(f"{item['kind']}: {clean(item['from'])} → {clean(item['to'])}", width, when))
            body = wrap(item["body"], width - 2)
            frame += [[("  " + line, "plain")] for line in body[: room - list_h - 1]]
    frame = frame[: height - 1]
    frame += [[]] * (height - 1 - len(frame))
    frame.append(fit(ui["footer"], width))
    return frame


# ---------------------------------------------------------------- terminal UI

def init_styles():
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    pairs = {
        "busy": curses.COLOR_GREEN, "ok": curses.COLOR_GREEN, "warn": curses.COLOR_YELLOW,
        "err": curses.COLOR_RED, "head": curses.COLOR_CYAN, "title": curses.COLOR_MAGENTA,
        "c0": curses.COLOR_CYAN, "c1": curses.COLOR_MAGENTA, "c2": curses.COLOR_YELLOW,
        "c3": curses.COLOR_BLUE, "c4": curses.COLOR_WHITE,
    }
    styles = {"plain": curses.A_NORMAL, "dim": curses.A_DIM, "sel": curses.A_BOLD}
    for i, (key, fg) in enumerate(pairs.items(), start=1):
        curses.init_pair(i, fg, bg)
        styles[key] = curses.color_pair(i)
    for key in ("busy", "err", "title", "head", "c0", "c1", "c2", "c3"):
        styles[key] |= curses.A_BOLD
    styles["title"] |= curses.A_REVERSE
    return styles


def style_of(styles, name):
    if name.endswith("+sel"):
        return styles.get(name[:-4], curses.A_NORMAL) | curses.A_REVERSE
    return styles.get(name, curses.A_NORMAL)


class App:
    def __init__(self, fleet, quota, terminals, route_cmd, claude_cmd, dry_run):
        self.fleet = fleet
        self.quota = quota
        self.terminals = terminals
        self.route_cmd = route_cmd
        self.claude_cmd = claude_cmd
        self.dry_run = dry_run
        self.ui = {"selected": None, "panel": "activity", "show_all": True, "scroll": 0, "top_scroll": 0,
                   "footer": [], "view": None, "view_mine": False, "view_scroll": 0, "view_pick": 0}
        self.selectable = []
        self.flash = ("", 0.0)
        self.mode = "normal"   # normal | task | plan | message
        self.buffer = ""
        self.task = None       # {"text", "cwd", "plan", "error"}
        self.targets = []      # sessions a message goes to
        self.armed = (None, 0.0)
        self.remotes = []

    def say(self, message):
        self.flash = (message, time.time())

    def selected_view(self, model):
        """The selected session, when a session row (not a helper, process or server row) is selected."""
        item = resolve(self.fleet, model, self.ui["selected"])
        return item[1] if item[0] == "session" else None

    def footer(self, now):
        if self.mode == "task":
            return [(" new task in ", "title"), (f" {short_path(self.task['cwd'])}: ", "warn"), (self.buffer + "_", "plain"),
                    ("   enter: plan it with route · esc: cancel", "dim")]
        if self.mode == "message":
            who = self.targets[0]["name"] if len(self.targets) == 1 else f"{len(self.targets)} sessions"
            return [(" message ", "title"), (f" {who}: ", "warn"), (self.buffer + "_", "plain"),
                    ("   enter: send · esc: cancel", "dim")]
        if self.mode == "plan":
            plan = self.task.get("plan") or {}
            if plan:
                removed = ", ".join(plan.get("removed_by_quota") or []) or "none"
                return [(" route ", "title"), (f" → {plan.get('chosen')} ", "ok"),
                        (f" {plan.get('shape')} · {plan.get('tier')} · {plan.get('why')}", "plain"),
                        (f" · quota removed: {removed}", "dim"),
                        ("   enter: dispatch in a new tab · c: start a Claude session instead · esc: cancel", "warn")]
            return [(" route ", "title"), (f" {self.task.get('error')} ", "err"),
                    ("   c: start a Claude session with this task · esc: cancel", "warn")]
        message, at = self.flash
        if message and now - at < FLASH_SECONDS:
            return [(" " + message, "warn")]
        return [(KEYS, "dim")]

    # -- keys

    def handle(self, key, model):
        if self.mode == "task":
            return self.edit(key, self.submit_task)
        if self.mode == "message":
            return self.edit(key, self.submit_message)
        if self.mode == "plan":
            return self.handle_plan_key(key)
        if self.ui["view"]:
            return self.handle_view_key(key)
        if key in ("q", "Q"):
            return False
        sel = self.selectable
        idx = sel.index(self.ui["selected"]) if self.ui["selected"] in sel else 0
        v = self.selected_view(model)
        if key in (curses.KEY_DOWN, "j") and sel:
            self.ui["selected"] = sel[min(len(sel) - 1, idx + 1)]
        elif key in (curses.KEY_UP, "k") and sel:
            self.ui["selected"] = sel[max(0, idx - 1)]
        elif key in (curses.KEY_NPAGE, "J"):
            self.ui["scroll"] += 10
        elif key in (curses.KEY_PPAGE, "K"):
            self.ui["scroll"] = max(0, self.ui["scroll"] - 10)
        elif key == "\t":
            order = ["activity", "session"] if self.ui.get("width", 0) >= 150 else ["activity", "session", "comms"]
            if self.ui["panel"] not in order:
                self.ui["panel"] = order[-1]
            self.ui["panel"] = order[(order.index(self.ui["panel"]) + 1) % len(order)]
        elif key in ("\n", "\r", curses.KEY_ENTER, "f"):
            item = resolve(self.fleet, model, self.ui["selected"])
            if item[0] in ("helper", "process"):
                self.ui.update(view="detail", view_scroll=0)
            elif item[0] == "remote":
                self.open_remote(item[1], item[2])
            else:
                self.jump(v)
        elif key == "p":
            self.ui["show_all"] = not self.ui["show_all"]
        elif key in ("c", "h", "?"):
            self.ui.update(view={"c": "comms", "h": "tree", "?": "help"}[key], view_scroll=0, view_pick=0,
                           view_mine=False)
        elif key in ("+", "="):
            self.say(self.quota.step(+1))
        elif key in ("-", "_"):
            self.say(self.quota.step(-1))
        elif key == "0":
            self.say(self.quota.clear())
        elif key == "n":
            self.task = {"cwd": v["cwd"] if v else os.getcwd(), "text": "", "plan": None, "error": None}
            self.buffer, self.mode = "", "task"
        elif key in ("C", "F"):
            self.session_command(v, {"C": "/closecode", "F": "/forkcode"}[key], key)
        elif key == "m":
            if v is None:
                return self.say("select a session row to message it (subagents are steered through their session)") or True
            self.start_message([v] if v else [])
        elif key == "M":
            peers = [p for p in ordered_sessions(model, True) if v and within(p["cwd"], v["cwd"])]
            self.start_message(peers)
        elif key == "x":
            item = resolve(self.fleet, model, self.ui["selected"])
            if item[0] == "process":
                self.stop(item)
            elif item[0] == "helper":
                self.say("a subagent stops with its session: select the session row, x x interrupts it")
            else:
                self.interrupt(v)
        return True

    def confirm(self, key, what):
        """True on the second press of `key` within a few seconds; the first press says what will happen."""
        armed, at = self.armed
        if armed != key or time.time() - at > CONFIRM_SECONDS:
            self.armed = (key, time.time())
            self.say(f"press {key[0]} again within {int(CONFIRM_SECONDS)}s to {what}")
            return False
        self.armed = (None, 0.0)
        return True

    def stop(self, item):
        host, agent = item[1], item[2]
        where = f" on {host['host']}" if host else ""
        if self.confirm(f"x:{agent['pid']}{where}", f"stop {agent['tool']} pid {agent['pid']}{where}"):
            self.say(stop_process(host["host"] if host else None, agent["pid"], self.dry_run))

    def session_command(self, v, command, key):
        if v is None or v["kind"] not in ("interactive", "herdr"):
            return self.say(f"select a live session row to run {command} in it")
        if v["group"] == "blocked":
            return self.say(f"{v['name']} is blocked on a prompt: answer it first (enter jumps there)")
        if self.confirm(f"{key}:{v['sid']}", f"type {command} into {v['name']}"):
            self.targets = [v]
            self.submit_message(command)

    def open_remote(self, host, v):
        ssh = shlex.split(os.environ.get("ATC_SSH") or "ssh")
        remote = f"cd {shlex.quote(v.get('cwd') or '~')} 2>/dev/null; exec $SHELL -l"
        command = " ".join(shlex.quote(p) for p in ssh + ["-t", host["host"], remote])
        self.say(open_tab(HOME, command, self.dry_run))

    def handle_view_key(self, key):
        ui = self.ui
        if key in ("\x1b", "q", "c", "h", "?") and not (key == "c" and ui["view"] != "comms") \
                and not (key == "h" and ui["view"] != "tree") and not (key == "?" and ui["view"] != "help"):
            ui["view"] = None
        elif key in ("c", "h", "?"):
            ui.update(view={"c": "comms", "h": "tree", "?": "help"}[key], view_scroll=0, view_pick=0, view_mine=False)
        elif key == "s":
            ui["view_mine"] = not ui["view_mine"]
            ui["view_pick"] = ui["view_scroll"] = 0
        elif key in (curses.KEY_DOWN, "j"):
            ui["view_pick"] += 1
            ui["view_scroll"] += 1
        elif key in (curses.KEY_UP, "k"):
            ui["view_pick"] = max(0, ui["view_pick"] - 1)
            ui["view_scroll"] = max(0, ui["view_scroll"] - 1)
        elif key == curses.KEY_NPAGE:
            ui["view_pick"] += 10
            ui["view_scroll"] += 10
        elif key == curses.KEY_PPAGE:
            ui["view_pick"] = max(0, ui["view_pick"] - 10)
            ui["view_scroll"] = max(0, ui["view_scroll"] - 10)
        return True

    def edit(self, key, submit):
        if key == "\x1b":
            self.mode = "normal"
        elif key in ("\n", "\r", curses.KEY_ENTER):
            text = self.buffer.strip()
            self.mode = "normal"
            if text:
                submit(text)
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            self.buffer = self.buffer[:-1]
        elif key == "\x15":  # ctrl-u
            self.buffer = ""
        elif isinstance(key, str) and key.isprintable():
            self.buffer += key
        return True

    # -- levers

    def jump(self, v):
        if not v:
            return self.say("nothing selected")
        if v["kind"] == "background":
            claude = " ".join(shlex.quote(p) for p in self.claude_cmd)
            return self.say(open_tab(v["cwd"], f"{claude} attach {shlex.quote(str(v['id']))}", self.dry_run))
        if v.get("herdr") and self.fleet.herdr:
            result = self.fleet.herdr.act(v["herdr"], "focus")
            client = next((p["tty"] for p in self.fleet.procs.values()
                           if p["tty"] and p["command"].split()[:1] and os.path.basename(p["command"].split()[0]) == "herdr"
                           and "server" not in p["command"]), "")
            if client:  # and bring the terminal running herdr to the front
                result += "; " + self.terminals.act(client, "focus")
            return self.say(result)
        self.say(self.terminals.act(v["tty"], "focus"))

    def start_message(self, targets):
        targets = [t for t in targets if t["kind"] in ("interactive", "herdr")]
        if not targets:
            return self.say("nothing to message (background sessions: enter attaches to them)")
        self.targets, self.buffer, self.mode = targets, "", "message"

    def submit_message(self, text):
        sent, skipped = [], []
        for v in self.targets:
            status = self.fleet.feed.status_now(v["sid"]) if self.fleet.feed and self.fleet.feed.cmd else v["status"]
            if (status or v["status"]) in ("waiting", "blocked") or v["group"] == "blocked":
                skipped.append(v["name"])  # typing would answer its permission prompt or question
                continue
            if v.get("herdr") and self.fleet.herdr:
                result = self.fleet.herdr.act(v["herdr"], "type", text)
            else:
                result = self.terminals.act(v["tty"], "type", text)
            (sent if result.startswith(("type: done", "dry run")) else skipped).append(
                v["name"] if result.startswith(("type: done", "dry run")) else f"{v['name']} ({result})")
        message = f"sent to {', '.join(sent)}" if sent else "sent to nobody"
        if skipped:
            message += f"; skipped {', '.join(skipped)} (blocked on a prompt: answer it there)"
        self.say(message)

    def interrupt(self, v):
        if not v:
            return self.say("nothing selected")
        verb = "stop the background session" if v["kind"] == "background" else "interrupt"
        if not self.confirm(f"x:{v['sid']}", f"{verb} {v['name']}"):
            return
        if v["kind"] == "background":
            if self.dry_run:
                return self.say(f"dry run: claude stop {v['id']}")
            code, _out, err = run(self.claude_cmd + ["stop", str(v["id"])])
            return self.say(f"stopped {v['name']}" if code == 0 else f"claude stop failed: {first_line(err)}")
        if v.get("herdr") and self.fleet.herdr:
            return self.say(self.fleet.herdr.act(v["herdr"], "interrupt"))
        self.say(self.terminals.act(v["tty"], "interrupt"))

    def submit_task(self, text):
        self.task["text"] = text
        self.task["plan"], self.task["error"] = route_plan(self.route_cmd, text, self.task["cwd"])
        self.mode = "plan"

    def handle_plan_key(self, key):
        task = self.task
        quoted = shlex.quote(task["text"])
        claude = " ".join(shlex.quote(p) for p in self.claude_cmd)
        if key == "\x1b":
            self.mode = "normal"
        elif key in ("c", "C"):
            self.say(open_tab(task["cwd"], f"{claude} {quoted}", self.dry_run))
            self.mode = "normal"
        elif key in ("\n", "\r", curses.KEY_ENTER) and task.get("plan"):
            chosen = task["plan"].get("chosen")
            if chosen == "claude":  # route keeps Claude-shaped work in a Claude session
                command = f"{claude} {quoted}"
            else:  # pin the pool route just showed, so a fresh sample can't pick a different one
                route = " ".join(shlex.quote(p) for p in self.route_cmd)
                command = f"{route} task --pool {shlex.quote(str(chosen))} {quoted}"
            self.say(f"{chosen}: " + open_tab(task["cwd"], command, self.dry_run))
            self.mode = "normal"
        return True


def draw(stdscr, app, model, styles):
    app.ui["footer"] = app.footer(time.time())
    height, width = stdscr.getmaxyx()
    frame, app.selectable = render(app.fleet, model, app.ui, width, height)
    if app.selectable and app.ui["selected"] not in app.selectable:
        app.ui["selected"] = app.selectable[0]
        frame, app.selectable = render(app.fleet, model, app.ui, width, height)
    if app.ui["view"]:
        frame = render_view(app.fleet, model, app.ui, width, height)
    stdscr.erase()
    for y, line in enumerate(frame[:height]):
        x = 0
        for text, style in line:
            if text and x < width:
                try:
                    stdscr.addstr(y, x, text[: width - x], style_of(styles, style))
                except curses.error:
                    pass  # writing the bottom-right cell raises; the text still lands
            x += len(text)
    stdscr.refresh()


def tui(stdscr, app):
    """Data refreshes once a second; keys redraw at once from the data we have.

    Input is waited for with select() and read without blocking, and the window size is checked on every pass:
    curses' own key timeout stops being honoured after the terminal grows, which froze the screen."""
    curses.curs_set(0)
    stdscr.nodelay(True)
    styles = init_styles()
    model, refreshed = None, 0.0
    debug = open(os.environ["ATC_DEBUG_LOG"], "a", buffering=1) if os.environ.get("ATC_DEBUG_LOG") else None
    while True:
        now = time.time()
        if model is None or now - refreshed >= TICK_SECONDS:
            app.fleet.refresh(now)
            model = app.fleet.model(now, app.quota, app.remotes)
            refreshed = now
            if debug:
                debug.write(f"{now:.2f} refresh {time.time() - now:.3f}s\n")
        try:
            size = os.get_terminal_size(sys.__stdout__.fileno())  # the tty itself, not $COLUMNS/$LINES
        except (OSError, ValueError, AttributeError):
            size = None
        if size and size.lines and size.columns and (size.lines, size.columns) != stdscr.getmaxyx():
            curses.resizeterm(size.lines, size.columns)
            stdscr.clear()
        draw(stdscr, app, model, styles)
        wait = min(0.25, max(0.0, TICK_SECONDS - (time.time() - refreshed)))
        try:
            select.select([sys.stdin], [], [], wait)
        except (OSError, ValueError):
            time.sleep(wait)
        keys = []
        while True:  # everything already typed (a held arrow key) before drawing again
            try:
                keys.append(stdscr.get_wch())
            except curses.error:
                break
        if debug:
            debug.write(f"{time.time():.2f} loop {time.time() - now:.3f}s keys={keys!r}\n")
        for key in keys:
            if key != curses.KEY_RESIZE and not app.handle(key, model):
                return


def main():
    parser = argparse.ArgumentParser(description="One terminal view of every Claude Code session on this machine.")
    parser.add_argument("--once", action="store_true", help="print one frame as plain text and exit")
    parser.add_argument("--tree", action="store_true", help="with --once: print the hierarchy instead")
    parser.add_argument("--comms", action="store_true", help="with --once: print every message instead")
    parser.add_argument("--json", action="store_true", help="print the fleet as JSON and exit")
    parser.add_argument("--here", action="store_true", help="only sessions in or under the current directory")
    parser.add_argument("--only", metavar="DIR", help="only sessions in or under DIR")
    parser.add_argument("--width", type=int, help="frame width for --once")
    parser.add_argument("--dry-run", action="store_true", help="levers say what they would do instead of doing it")
    parser.add_argument("--jump", metavar="SESSION_ID", help="focus that session's terminal tab (attach a background one)")
    parser.add_argument("--cap", choices=["up", "down", "auto"], help="raise, lower or reset the swarm cap")
    parser.add_argument("--open", action="store_true", help="open the live view in a new terminal tab")
    parser.add_argument("--host", action="append", default=[], metavar="SSH_TARGET",
                        help=f"also show this machine over ssh (repeatable; or list them in {short_path(HOSTS_FILE)})")
    parser.add_argument("--no-hosts", action="store_true", help="this machine only")
    args = parser.parse_args()

    only = os.path.abspath(os.path.expanduser(args.only)) if args.only else (os.getcwd() if args.here else None)
    quota_cmd = command_from_env("ATC_QUOTAMAX", "quotamax")
    route_cmd = command_from_env("ATC_ROUTE", "route")
    claude_cmd = command_from_env("ATC_CLAUDE", "claude") or ["claude"]
    one_shot = args.once or args.json or args.tree or args.comms or args.jump

    if args.open:
        print(open_tab(os.getcwd(), shlex.quote(os.path.abspath(__file__)), args.dry_run))
        return
    if args.cap:
        quota = Quota(quota_cmd, args.dry_run, background=False)
        if quota_cmd:
            quota.fetch()
        print({"up": lambda: quota.step(+1), "down": lambda: quota.step(-1), "auto": quota.clear}[args.cap]())
        return

    feed = Feed(claude_cmd if shutil.which(claude_cmd[0]) else None, background=not one_shot)
    herdr = Herdr(command_from_env("ATC_HERDR", "herdr"), args.dry_run, background=not one_shot)
    repos = Repos(background=not one_shot)
    fleet = Fleet(only, feed, herdr, Procs(background=not one_shot), repos)
    quota = Quota(quota_cmd, args.dry_run, background=not one_shot)
    hosts = [] if args.no_hosts or only else configured_hosts(args.host)
    remotes = [Remote(h, background=not one_shot) for h in hosts]

    if one_shot:
        workers = [threading.Thread(target=r.fetch) for r in remotes if not args.jump]
        for w in workers:
            w.start()
        feed.fetch()
        herdr.fetch()
        if quota_cmd:
            quota.fetch()
        for w in workers:
            w.join()
        now = time.time()
        fleet.refresh(now)
        repos.fetch()
        model = fleet.model(now, quota, remotes)
        if args.jump:
            target = next((v for v in ordered_sessions(model, True) if v["sid"] == args.jump), None)
            if target is None:
                print(f"no live session {args.jump}")
                sys.exit(1)
            app = App(fleet, quota, Terminals(args.dry_run), route_cmd, claude_cmd, args.dry_run)
            app.jump(target)
            print(app.flash[0])
            done = app.flash[0].startswith(("focus: done", "opened", "dry run", "run this yourself"))
            sys.exit(0 if done else 1)  # a launcher (Roost) shows only failures
        if args.json:
            json.dump(dict(model, quota=quota.data), sys.stdout, indent=2, default=str)
            print()
            return
        width = args.width or shutil.get_terminal_size((140, 40)).columns
        if args.tree:
            frame = tree_lines(fleet, model)
        elif args.comms:
            frame = [comm_line(item) for item in reversed(model["comms"])]
        else:
            frame, _ = render(fleet, model, {"selected": None, "show_all": True}, width)
        for line in frame:
            print("".join(text for text, _style in fit(line, width)).rstrip())
        return

    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")  # curses waits a full second after ESC by default
    print("atc: reading Claude Code sessions…", flush=True)
    feed.fetch()
    app = App(fleet, quota, Terminals(args.dry_run), route_cmd, claude_cmd, args.dry_run)
    app.remotes = remotes
    try:
        curses.wrapper(tui, app)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
