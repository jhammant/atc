"""atc as an MCP server: `atc mcp` over stdio driven by a fake client, and POST /mcp on --serve, against the fake
Claude home in fixture.py. Every read tool, every lever in dry run, the prompt-blocked guard, confirmation (both
protocol eras), the self guard, the lever policy, the audit log, a lever run on a server over (fake) ssh, and an
always-on atc standing in for one that sleeps.

Run from the repo root:  python3 -m unittest discover -s tests -p test_mcp.py -v
"""
import itertools
import json
import os
import queue
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixture  # noqa: E402
from test_units import load_atc  # noqa: E402

FX = None
SAVED_ENV = {}
MODERN = "2026-07-28"
ASKS = {"elicitation": {"form": {}}}
FAKE_SSH = '''#!/usr/bin/env python3
"""ssh that runs the remote half here, and says it is another machine."""
import json, subprocess, sys
args = sys.argv[1:]
while args and args[0] == "-o":
    args = args[2:]
host, cmd = args[0], args[1:]
res = subprocess.run(cmd, stdin=sys.stdin, capture_output=True)
out = res.stdout
if "--json" in cmd:
    data = json.loads(out)
    data["machine"] = "fake-" + host
    out = json.dumps(data).encode()
sys.stdout.buffer.write(out)
sys.stderr.buffer.write(res.stderr)
sys.exit(res.returncode)
'''


def setUpModule():
    global FX
    if not fixture.TMUX:
        raise unittest.SkipTest("tmux is needed for the fixture's live sessions")
    FX = fixture.Fixture().start()
    SAVED_ENV.update(os.environ)
    env = FX.env()
    os.environ.clear()
    os.environ.update(env)


def tearDownModule():
    if SAVED_ENV:
        os.environ.clear()
        os.environ.update(SAVED_ENV)
    if FX:
        FX.stop()


def meta(caps=None):
    return {"io.modelcontextprotocol/protocolVersion": MODERN,
            "io.modelcontextprotocol/clientInfo": {"name": "test-client", "version": "0"},
            "io.modelcontextprotocol/clientCapabilities": ASKS if caps is None else caps}


def text(result):
    return result["content"][0]["text"]


class Client:
    """A fake MCP client: starts `atc mcp`, writes JSON-RPC lines, reads them back."""

    def __init__(self, **env):
        base = {"CLAUDE_CODE_SESSION_ID": "", "CLAUDE_CODE_SESSION_ATTENDED": "1", "CLAUDE_CODE_ENTRYPOINT": "cli",
                "ATC_MCP_LEVERS": ""}
        self.proc = subprocess.Popen([sys.executable, fixture.ATC, "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, env=FX.env(**dict(base, **env)))
        self.lines = queue.Queue()
        self.ids = itertools.count(1)
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        for line in self.proc.stdout:
            self.lines.put(line)

    def send(self, message):
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def recv(self, timeout=90):
        return json.loads(self.lines.get(timeout=timeout))

    def request(self, method, params=None, caps=None, modern=True):
        mid = next(self.ids)
        params = dict(params or {})
        if modern:
            params["_meta"] = meta(caps)
        self.send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        reply = self.recv()
        assert reply.get("id") == mid, reply
        return reply

    def call(self, name, arguments=None, caps=None, modern=True, **extra):
        reply = self.request("tools/call", dict({"name": name, "arguments": arguments or {}}, **extra), caps, modern)
        assert "result" in reply, reply
        return reply["result"]

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=20)
        self.proc.stdout.close()
        self.proc.stderr.close()


class Stdio(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = Client()

    @classmethod
    def tearDownClass(cls):
        cls.c.close()

    def test_discover_and_list(self):
        found = self.c.request("server/discover")["result"]
        self.assertEqual(found["resultType"], "complete")
        self.assertIn(MODERN, found["supportedVersions"])
        self.assertIn("2025-11-25", found["supportedVersions"])
        listed = self.c.request("tools/list")["result"]
        for result in (found, listed):  # 2026-07-28 requires caching hints on both; Claude Code rejects a list without
            self.assertGreaterEqual(result["ttlMs"], 0)
            self.assertIn(result["cacheScope"], ("public", "private"))
        tools = {t["name"]: t for t in listed["tools"]}
        self.assertEqual(len(tools), 21)
        for name, t in tools.items():
            self.assertEqual(t["inputSchema"]["type"], "object", name)
            lever = "dry_run" in t["inputSchema"]["properties"]
            self.assertEqual(t["annotations"]["readOnlyHint"], not lever, name)
        self.assertIn("atc_needs_me", tools)
        self.assertTrue(tools["atc_later"]["annotations"]["readOnlyHint"])
        self.assertTrue(tools["atc_jobs"]["annotations"]["readOnlyHint"])
        self.assertEqual(sum(t["annotations"]["readOnlyHint"] for t in tools.values()), 9)
        self.assertTrue(tools["atc_interrupt"]["annotations"]["destructiveHint"])

    def test_handshake_revision(self):
        c = Client()
        try:
            c.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "old"}}})
            init = c.recv()["result"]
            self.assertEqual(init["protocolVersion"], "2025-06-18")
            self.assertIn("tools", init["capabilities"])
            c.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            tools = c.request("tools/list", modern=False)["result"]
            self.assertNotIn("resultType", tools)
            self.assertEqual(len(tools["tools"]), 21)
            c.send({"jsonrpc": "2.0", "id": 99, "method": "initialize", "params": {"protocolVersion": "2024-01-01"}})
            self.assertEqual(c.recv()["result"]["protocolVersion"], "2025-11-25")
        finally:
            c.close()

    def test_protocol_errors(self):
        self.c.send({"jsonrpc": "2.0", "id": "v", "method": "tools/list", "params": {"_meta": {
            "io.modelcontextprotocol/protocolVersion": "1900-01-01", "io.modelcontextprotocol/clientCapabilities": {}}}})
        bad = self.c.recv()["error"]
        self.assertEqual(bad["code"], -32022)
        self.assertIn(MODERN, bad["data"]["supported"])
        self.c.send({"jsonrpc": "2.0", "id": "c", "method": "tools/list", "params": {"_meta": {
            "io.modelcontextprotocol/protocolVersion": MODERN}}})
        self.assertEqual(self.c.recv()["error"]["code"], -32602)
        self.assertEqual(self.c.request("tools/call", {"name": "atc_nope"})["error"]["code"], -32602)
        self.assertEqual(self.c.request("resources/list")["error"]["code"], -32601)
        self.c.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}})
        self.assertEqual(self.c.request("ping")["result"]["resultType"], "complete")  # still answering

    def test_fleet(self):
        out = text(self.c.call("atc_fleet"))
        for expected in ("Blocked on a prompt (1)", "gamma-33", "changelog-drafter", "Working (1)", "Write the tests",
                         "release-train", "Parked", "bg deadbeef", "codex", "closed sessions can be resumed",
                         "comfortable"):
            self.assertIn(expected, out)
        only = text(self.c.call("atc_fleet", {"folder": os.path.join(FX.work, "alpha")}))
        self.assertIn("alpha", only)
        self.assertNotIn("gamma-33", only)

    def test_needs_me(self):
        out = text(self.c.call("atc_needs_me"))
        self.assertIn("Blocked on a prompt (1)", out)
        self.assertIn("asks: permission prompt", out)
        self.assertIn("changelog-drafter", out)
        self.assertIn("said: Done. Want me to ship it?", out)
        self.assertIn("1 working", out)

    def test_session(self):
        beta = text(self.c.call("atc_session", {"session": "changelog-drafter"}))
        self.assertIn("beta-22", beta)
        self.assertIn(FX.sid("beta-22")[:8], beta)
        self.assertIn("Want me to ship it?", beta)
        alpha = text(self.c.call("atc_session", {"session": FX.sid("alpha-11")}))
        self.assertIn("Write the tests", alpha)
        self.assertIn("recent messages", alpha)
        self.assertIn("Run the test suite", alpha)  # its activity
        self.assertEqual(text(self.c.call("atc_session", {"session": "alpha"})).split()[0], "Alpha")

    def test_session_lookup_errors(self):
        missing = self.c.call("atc_session", {"session": "nope"})
        self.assertTrue(missing["isError"])
        self.assertIn("no live session", text(missing))
        several = self.c.call("atc_session", {"session": "0000000"})
        self.assertTrue(several["isError"])
        self.assertIn("matches", text(several))
        self.assertTrue(self.c.call("atc_session", {"session": "x", "host": "nowhere"})["isError"])

    def test_tree_comms_quota_closed(self):
        self.assertIn("workflow release-train", text(self.c.call("atc_tree")))
        self.assertIn("alpha-11", text(self.c.call("atc_tree", {"session": "alpha-11"})))
        comms = text(self.c.call("atc_comms"))
        self.assertIn("✉@ alpha-11 → beta-22", comms)
        self.assertEqual(comms.count("API review by a-mail"), 1)
        full = text(self.c.call("atc_comms", {"session": "beta-22", "full": True}))
        self.assertIn("Please review the API before we ship", full)
        self.assertIn("Check the error paths.", full)
        self.assertIn("The error paths look good.", full)
        self.assertNotIn("Smoke test staging", full)
        quota = text(self.c.call("atc_quota"))
        self.assertIn("swarm cap", quota)
        self.assertIn("up to 4 parallel agents", quota)
        closed = text(self.c.call("atc_closed"))
        self.assertIn("api-client", closed)
        self.assertIn("next: Deploy to staging", closed)


class Levers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = Client()

    @classmethod
    def tearDownClass(cls):
        cls.c.close()

    def setUp(self):
        FX.clear_calls()

    def dry(self, name, arguments):
        result = self.c.call(name, dict(arguments, dry_run=True))
        self.assertFalse(result["isError"], text(result))
        self.assertIn("dry run", text(result))
        return text(result)

    def test_every_lever_in_dry_run(self):
        before = {n: FX.pane(n) for n in ("alpha-11", "beta-22")}
        self.assertIn("tmux", self.dry("atc_message", {"session": "beta-22", "text": "dry hello"}))
        out = self.dry("atc_broadcast", {"folder": FX.work, "text": "dry all"})
        self.assertIn("gamma-33", out)  # skipped: blocked on its prompt
        self.dry("atc_interrupt", {"session": "alpha-11"})
        self.assertIn("claude stop deadbeef", self.dry("atc_interrupt", {"session": "bg deadbeef"}))
        self.assertIn(str(FX.stray), self.dry("atc_stop_process", {"pid": FX.stray}))
        self.assertIn("/closecode", self.dry("atc_close", {"session": "beta-22"}))
        self.assertIn("/forkcode", self.dry("atc_fork", {"session": "beta-22"}))
        self.assertIn("/rename change-log", self.dry("atc_rename", {"session": "beta-22", "name": "Change Log!"}))
        self.assertIn(f"--resume {FX.closed_sid}", self.dry("atc_restore", {"session": FX.closed_sid[:8]}))
        self.assertIn("override abundant", self.dry("atc_cap", {"direction": "up"}))
        self.assertIn("task --pool codex", self.dry("atc_new_task", {"task": "write tests", "folder": FX.work,
                                                                     "pool": "codex"}))
        self.dry("atc_jump", {"session": "gamma-33"})
        self.assertNotIn("quotamax override", FX.calls())
        time.sleep(0.5)
        self.assertEqual({n: FX.pane(n) for n in before}, before)  # nothing typed anywhere

    def test_plans_and_free_levers_run_unasked(self):
        plan = self.c.call("atc_new_task", {"task": "write tests", "folder": FX.work})
        self.assertIn("route says: codex", text(plan))
        self.assertIn("route task --plan-only write tests", FX.calls())
        cap = self.c.call("atc_cap", {"direction": "up"})
        self.assertNotEqual(cap.get("resultType"), "input_required")  # the cap is a free lever: nobody is asked
        self.assertIn("abundant", text(cap))
        self.c.call("atc_cap", {"direction": "auto"})
        self.assertIn("quotamax override abundant 2", FX.calls())

    def test_refusals(self):
        blocked = self.c.call("atc_message", {"session": "gamma-33", "text": "must not arrive"})
        self.assertTrue(blocked["isError"])
        self.assertIn("blocked on a prompt", text(blocked))
        self.assertTrue(self.c.call("atc_close", {"session": "gamma-33", "dry_run": True})["isError"])
        unsaved = self.c.call("atc_close", {"session": "beta-22", "how": "exit"})
        self.assertTrue(unsaved["isError"])
        self.assertIn("unsaved work", text(unsaved))
        self.assertIn("atc_close how: save", text(unsaved))
        self.assertIn("isn't an agent process", text(self.c.call("atc_stop_process", {"pid": 1})))
        self.assertIn("background", text(self.c.call("atc_message", {"session": "bg deadbeef", "text": "x"})))
        self.assertTrue(self.c.call("atc_message", {"session": "beta-22", "text": "  "})["isError"])
        time.sleep(0.5)
        self.assertNotIn("must not arrive", FX.pane("gamma-33"))

    def test_confirmation(self):
        args = {"session": "beta-22", "text": "hello from mcp"}
        asked = self.c.call("atc_message", args)
        self.assertEqual(asked["resultType"], "input_required")
        question = asked["inputRequests"]["confirm"]
        self.assertEqual(question["method"], "elicitation/create")
        self.assertIn("hello from mcp", question["params"]["message"])
        self.assertIn("beta-22", question["params"]["message"])
        time.sleep(0.3)
        self.assertNotIn("hello from mcp", FX.pane("beta-22"))
        yes = {"confirm": {"action": "accept", "content": {"confirm": True}}}
        done = self.c.call("atc_message", args, inputResponses=yes, requestState=asked["requestState"])
        self.assertFalse(done["isError"], text(done))
        self.assertIn("typed into", text(done))
        time.sleep(0.5)
        self.assertIn("hello from mcp", FX.pane("beta-22"))
        again = self.c.call("atc_message", args, inputResponses=yes, requestState=asked["requestState"])
        self.assertEqual(again["resultType"], "input_required")  # each yes pulls the lever once
        forged = asked["requestState"][:-4] + "beef"
        self.assertEqual(self.c.call("atc_message", args, inputResponses=yes, requestState=forged)["resultType"],
                         "input_required")
        other = self.c.call("atc_message", dict(args, text="something else"), inputResponses=yes,
                            requestState=self.c.call("atc_message", args)["requestState"])
        self.assertEqual(other["resultType"], "input_required")  # a yes covers the call it was asked about only
        fresh = self.c.call("atc_message", dict(args, text="declined text"))
        no = self.c.call("atc_message", dict(args, text="declined text"), requestState=fresh["requestState"],
                         inputResponses={"confirm": {"action": "decline"}})
        self.assertTrue(no["isError"])
        self.assertIn("Not confirmed (decline)", text(no))
        time.sleep(0.5)
        self.assertNotIn("declined text", FX.pane("beta-22"))

    def test_no_way_to_ask(self):
        pending = self.c.call("atc_interrupt", {"session": "alpha-11"}, caps={})
        self.assertFalse(pending["isError"])
        self.assertIn("Waiting for approval", text(pending))

    def test_audit_log(self):
        self.c.call("atc_message", {"session": "gamma-33", "text": "audited"})
        path = os.path.join(FX.root, "home", ".config", "atc", "audit.jsonl")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        with open(path) as fh:
            rows = [json.loads(line) for line in fh]
        last = rows[-1]
        self.assertEqual((last["tool"], last["outcome"], last["transport"]), ("atc_message", "refused", "stdio"))
        self.assertEqual(last["arguments"]["text"], "audited")
        self.assertEqual(last["client"], "test-client")


class Policy(unittest.TestCase):
    def test_handshake_client_is_asked_with_elicitation_create(self):
        c = Client()
        try:
            c.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {"elicitation": {}}, "clientInfo": {"name": "old"}}})
            c.recv()
            c.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "atc_message", "arguments": {"session": "beta-22", "text": "via legacy ask"}}})
            ask = c.recv()
            self.assertEqual(ask["method"], "elicitation/create")
            self.assertIn("via legacy ask", ask["params"]["message"])
            c.send({"jsonrpc": "2.0", "id": ask["id"], "result": {"action": "accept", "content": {"confirm": True}}})
            done = c.recv()
            self.assertEqual(done["id"], 2)
            self.assertFalse(done["result"]["isError"], done)
            time.sleep(0.5)
            self.assertIn("via legacy ask", FX.pane("beta-22"))
        finally:
            c.close()

    def test_self_guard(self):
        c = Client(CLAUDE_CODE_SESSION_ID=FX.sid("beta-22"))
        try:
            mine = c.call("atc_message", {"session": "beta-22", "text": "to myself"})
            self.assertTrue(mine["isError"])
            self.assertIn("the session calling", text(mine))
            meant = c.call("atc_message", {"session": "beta-22", "text": "to myself", "allow_self": True,
                                           "dry_run": True})
            self.assertFalse(meant["isError"], text(meant))
            out = text(c.call("atc_broadcast", {"folder": FX.work, "text": "x", "dry_run": True}))
            self.assertNotIn("beta-22", out.split("typed into")[1].split(";")[0])
        finally:
            c.close()

    def test_unattended_session_requests_owner_approval(self):
        c = Client(CLAUDE_CODE_SESSION_ATTENDED="0")
        try:
            pending = c.call("atc_message", {"session": "beta-22", "text": "nobody watching"})
            self.assertFalse(pending["isError"])
            self.assertIn("Waiting for approval", text(pending))
            self.assertNotIn("nobody watching", FX.pane("beta-22"))
            self.assertFalse(c.call("atc_jump", {"session": "beta-22", "dry_run": True})["isError"])
        finally:
            c.close()

    def test_headless_run_requests_owner_approval(self):
        c = Client(CLAUDE_CODE_ENTRYPOINT="sdk-cli")  # claude -p: says attended, can't show a form
        try:
            pending = c.call("atc_message", {"session": "beta-22", "text": "from a headless run"})
            self.assertFalse(pending["isError"])
            self.assertIn("Waiting for approval", text(pending))
            self.assertNotIn("from a headless run", FX.pane("beta-22"))
        finally:
            c.close()

    def test_levers_off_and_auto(self):
        off = Client(ATC_MCP_LEVERS="off")
        try:
            names = [t["name"] for t in off.request("tools/list")["result"]["tools"]]
            self.assertEqual(len(names), 9)  # read tools, including atc_jobs and atc_later
            self.assertNotIn("atc_message", names)
            self.assertIn("levers are off", text(off.call("atc_message", {"session": "beta-22", "text": "x"})))
        finally:
            off.close()
        auto = Client(ATC_MCP_LEVERS="auto")
        try:
            done = auto.call("atc_message", {"session": "beta-22", "text": "auto says hi"})
            self.assertFalse(done["isError"], text(done))
            time.sleep(0.5)
            self.assertIn("auto says hi", FX.pane("beta-22"))
        finally:
            auto.close()

    def test_lever_on_a_server_runs_there(self):
        fake_ssh = os.path.join(FX.stub, "fake-ssh-mcp")
        with open(fake_ssh, "w") as fh:
            fh.write(FAKE_SSH.replace("/usr/bin/env python3", sys.executable, 1))
        os.chmod(fake_ssh, 0o755)
        c = Client(ATC_HOSTS="box1", ATC_SSH=fake_ssh, ATC_MCP_LEVERS="auto")
        try:
            self.assertIn("box1", text(c.call("atc_fleet")))
            both = c.call("atc_message", {"session": "beta-22", "text": "which one"})
            self.assertTrue(both["isError"])
            self.assertIn("on box1", text(both))  # here and on the server: name one
            there = c.call("atc_message", {"session": "beta-22@box1", "text": "typed over ssh"})
            self.assertFalse(there["isError"], text(there))
            time.sleep(0.5)
            self.assertIn("typed over ssh", FX.pane("beta-22"))
            blocked = c.call("atc_message", {"session": "gamma-33", "host": "box1", "text": "no"})
            self.assertIn("blocked on a prompt", text(blocked))
            self.assertIn("dry run", text(c.call("atc_stop_process", {"pid": FX.stray, "host": "box1",
                                                                       "dry_run": True})))
        finally:
            c.close()


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_atc(FX.env())
        cls.token = "test-token-not-secret"
        cls.servers = []
        cls.url = cls.start(cls.service(writes=True))
        cls.read_url = cls.start(cls.service(writes=False))

    @classmethod
    def service(cls, writes, only=None, prefer=None, remotes=()):
        m = cls.m
        feed = m.Feed([os.path.join(FX.stub, "claude")], background=False)
        feed.fetch()
        fleet = m.Fleet(only, feed, m.Herdr(None, background=False), repos=m.Repos(background=False))
        fleet.refresh(time.time())
        quota = m.Quota([os.path.join(FX.stub, "quotamax")], background=False)
        quota.fetch()
        service = m.Service(fleet, quota, list(remotes), m.Terminals(), writes=writes, token=cls.token, background=False,
                            claude_cmd=[os.path.join(FX.stub, "claude")], route_cmd=[os.path.join(FX.stub, "route")],
                            prefer=prefer)
        service.refresh()
        return service

    @classmethod
    def start(cls, service):
        server = cls.m.make_server(service, "127.0.0.1:0")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        cls.servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        for server in cls.servers:
            server.shutdown()
            server.server_close()

    def post(self, body, url=None, token="default", headers=None, raw=None):
        token = self.token if token == "default" else token
        params = body.get("params") or {}
        hdrs = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if "_meta" in params:
            hdrs.update({"MCP-Protocol-Version": MODERN, "Mcp-Method": body.get("method")})
            if body.get("method") == "tools/call":
                hdrs["Mcp-Name"] = params.get("name")
        if token:
            hdrs["Authorization"] = f"Bearer {token}"
        hdrs.update(headers or {})
        req = urllib.request.Request((url or self.url) + "/mcp", data=raw or json.dumps(body).encode(),
                                     headers={k: v for k, v in hdrs.items() if v is not None}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
                return r.status, json.loads(data) if data else None
        except urllib.error.HTTPError as e:
            with e:
                data = e.read()
            return e.code, json.loads(data) if data else None

    @staticmethod
    def rpc(method, params=None, modern=True, mid=1, caps=None):
        params = dict(params or {})
        if modern:
            params["_meta"] = meta(caps)
        return {"jsonrpc": "2.0", "id": mid, "method": method, "params": params}

    def test_token_is_always_needed(self):
        body = self.rpc("tools/list")
        self.assertEqual(self.post(body, token=None)[0], 401)
        self.assertEqual(self.post(body, token="wrong")[0], 401)
        self.assertEqual(self.post(body)[0], 200)

    def test_read_tools(self):
        status, reply = self.post(self.rpc("tools/list"))
        self.assertEqual((status, len(reply["result"]["tools"])), (200, 21))
        status, reply = self.post(self.rpc("tools/call", {"name": "atc_needs_me", "arguments": {}}))
        self.assertEqual(status, 200)
        self.assertIn("gamma-33", text(reply["result"]))

    def test_transport_rules(self):
        body = self.rpc("tools/list")
        self.assertEqual(self.post(body, headers={"Mcp-Method": "tools/call"})[1]["error"]["code"], -32020)
        self.assertEqual(self.post(body, headers={"MCP-Protocol-Version": None})[0], 400)
        call = self.rpc("tools/call", {"name": "atc_fleet", "arguments": {}})
        self.assertEqual(self.post(call, headers={"Mcp-Name": "atc_quota"})[0], 400)
        self.assertEqual(self.post(call, headers={"Mcp-Name": "=?base64?YXRjX2ZsZWV0?="})[0], 200)
        old = {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {"_meta": {
            "io.modelcontextprotocol/protocolVersion": "1900-01-01", "io.modelcontextprotocol/clientCapabilities": {}}}}
        status, reply = self.post(old, headers={"MCP-Protocol-Version": "1900-01-01"})
        self.assertEqual((status, reply["error"]["code"]), (400, -32022))
        self.assertEqual(self.post(self.rpc("prompts/list"))[0], 404)
        self.assertEqual(self.post({"jsonrpc": "2.0", "method": "notifications/initialized"}), (202, None))
        self.assertEqual(self.post({}, raw=b"{nope")[0], 400)
        self.assertEqual(self.post(body, headers={"Origin": "https://evil.example"})[0], 403)
        get = urllib.request.Request(self.url + "/mcp", headers={"Authorization": f"Bearer {self.token}"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(get, timeout=10)
        caught.exception.close()
        self.assertEqual(caught.exception.code, 405)

    def test_handshake_revision_over_http(self):
        status, init = self.post(self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}, False))
        self.assertEqual((status, init["result"]["protocolVersion"]), (200, "2025-06-18"))
        status, tools = self.post(self.rpc("tools/list", modern=False), headers={"MCP-Protocol-Version": "2025-06-18"})
        self.assertEqual((status, len(tools["result"]["tools"])), (200, 21))
        status, lever = self.post(self.rpc("tools/call", {"name": "atc_message", "arguments": {
            "session": "beta-22", "text": "legacy http"}}, False))
        self.assertEqual(status, 200)
        self.assertFalse(lever["result"]["isError"])
        row = self.m.Approvals().access()[-1]
        self.assertEqual((row["tool"], row["state"]), ("atc_message", "pending"))
        self.assertIn("Waiting for approval " + row["id"], text(lever["result"]))
        self.assertNotIn("legacy http", FX.pane("beta-22"))

    def test_levers_need_serve_writes(self):
        names = [t["name"] for t in self.post(self.rpc("tools/list"), url=self.read_url)[1]["result"]["tools"]]
        self.assertEqual(len(names), 9)  # read tools, including atc_jobs and atc_later
        reply = self.post(self.rpc("tools/call", {"name": "atc_message", "arguments": {"session": "beta-22",
                                                                                        "text": "x"}}), url=self.read_url)
        self.assertIn("levers are off", text(reply[1]["result"]))

    def test_confirm_and_self_guard_over_http(self):
        args = {"session": "beta-22", "text": "hello over http"}
        asked = self.post(self.rpc("tools/call", {"name": "atc_message", "arguments": args}))[1]["result"]
        self.assertEqual(asked["resultType"], "input_required")
        reply = self.post(self.rpc("tools/call", {"name": "atc_message", "arguments": args, "requestState": asked[
            "requestState"], "inputResponses": {"confirm": {"action": "accept", "content": {"confirm": True}}}}))
        self.assertFalse(reply[1]["result"]["isError"], reply)
        time.sleep(0.5)
        self.assertIn("hello over http", FX.pane("beta-22"))
        mine = self.post(self.rpc("tools/call", {"name": "atc_message", "arguments": args}),
                         headers={"X-Atc-Caller": FX.sid("beta-22")})[1]["result"]
        self.assertIn("the session calling", text(mine))

    def test_proxied_reads_need_the_token(self):
        def get(headers):
            req = urllib.request.Request(self.url + "/api/taxi/sessions", headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                e.close()
                return e.code
        self.assertEqual(get({}), 200)
        self.assertEqual(get({"X-Forwarded-For": "203.0.113.9"}), 401)
        self.assertEqual(get({"X-Forwarded-For": "203.0.113.9", "Authorization": f"Bearer {self.token}"}), 200)

    def test_stands_in_while_the_preferred_atc_sleeps(self):
        self.m.PREFER_CHECK_EVERY = 0
        home = self.service(writes=True)
        home_server = self.m.make_server(home, "127.0.0.1:0")
        threading.Thread(target=home_server.serve_forever, daemon=True).start()
        home_url = f"http://127.0.0.1:{home_server.server_address[1]}"
        home_view = self.m.RemoteAtc(home_url, self.token, background=False)
        home_view.fetch()  # while it is awake: what the stand-in will show once it sleeps
        self.assertTrue(home_view.data and home_view.data["counts"]["blocked"] == 1, home_view.error)
        home_view.data["machine"] = home_view.host = "home-box"  # it is this machine here; in use it is another
        standin = self.service(writes=True, only="/nowhere/at/all", prefer=home_url, remotes=[home_view])
        standin_url = self.start(standin)
        standin.refresh()  # its own loop checks the preferred atc, with or without MCP traffic
        self.assertTrue(standin.prefer_ok)
        self.assertNotIn("hasn't answered", standin.cockpit.notice())
        needs = self.rpc("tools/call", {"name": "atc_needs_me", "arguments": {}})
        awake = text(self.post(needs, url=standin_url)[1]["result"])
        self.assertIn("gamma-33", awake)  # passed through to the home atc, which sees the sessions
        self.assertNotIn("hasn't answered", awake)
        args = {"session": "beta-22", "text": "confirmed through the stand-in"}
        asked = self.post(self.rpc("tools/call", {"name": "atc_message", "arguments": args}), url=standin_url)
        state = asked[1]["result"]["requestState"]
        done = self.post(self.rpc("tools/call", {"name": "atc_message", "arguments": args, "requestState": state,
                                                 "inputResponses": {"confirm": {"action": "accept"}}}), url=standin_url)
        self.assertFalse(done[1]["result"]["isError"], done)
        home_server.shutdown()
        home_server.server_close()
        home_view.fetch()
        standin.refresh()
        asleep = text(self.post(needs, url=standin_url)[1]["result"])
        self.assertIn("hasn't answered", asleep)
        self.assertNotIn("Nobody needs you", asleep)  # the shared queue retains the sleeping host's items
        self.assertIn("home-box isn't answering", asleep)
        self.assertIn("Last seen", asleep)
        self.assertIn("gamma-33", asleep)
        fleet = text(self.post(self.rpc("tools/call", {"name": "atc_fleet", "arguments": {}}), url=standin_url)[1]["result"])
        self.assertIn("as last seen", fleet)
        self.assertIn("gamma-33", fleet)
        steer = self.post(self.rpc("tools/call", {"name": "atc_message", "arguments": {
            "session": "beta-22@home-box", "text": "while it sleeps"}}), url=standin_url)[1]["result"]
        self.assertTrue(steer["isError"])
        self.assertIn("can't be steered until it is back", text(steer))
        time.sleep(0.5)
        self.assertIn("confirmed through the stand-in", FX.pane("beta-22"))


if __name__ == "__main__":
    unittest.main()
