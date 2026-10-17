"""Approval transitions, real HTTP/passkey handoff and terminal controls, without live agent processes."""
import concurrent.futures
import contextlib
import io
import json
import os
import pty
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import fixture
from test_units import load_atc
from test_web import HTTP, OPENSSL, ORIGIN, PEER, TOKEN


class ApprovalCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="atc-approvals-")
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"HOME": self.tmp.name, "ATC_PEER_TOKEN": "",
                                          "ATC_MCP_LEVERS": "confirm", "ATC_NOTIFY_CMD": "", "ATC_APPROVAL_WAIT": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.m = load_atc({})
        self.store = self.m.Approvals()
        self.args = {"session": "beta-22", "text": "the private message"}
        self.ctx = {"modern": True, "caps": {}, "client": {"name": "test-client"}, "version": "2026-07-28"}
        self.service = self.make_service()

    def make_service(self, name="hub", web=False):
        store = self.store if name == "hub" else self.m.Approvals(os.path.join(self.tmp.name, name))
        fleet = self.m.Fleet(approvals=store)
        service = self.m.Service(fleet, None, [], self.m.Terminals(), writes=True, token=TOKEN,
                                 background=False, peer_secret=PEER, web_origin=ORIGIN if web else None,
                                 web_dir=os.path.join(self.tmp.name, name + "-web"))
        service.model = fleet.model(time.time())
        service.model["machine"] = name
        service.model["groups"]["waiting"] = [{"sid": "beta-22", "name": "beta", "kind": "interactive",
                                                "group": "waiting", "status": "idle", "cwd": "/workspace/example",
                                                "doing": "done", "since": 10}]
        return service

    def call(self, args=None, caller="alpha-11", server=None, ctx=None, name="atc_message", **extra):
        return (server or self.service.mcp).call_tool(
            dict(name=name, arguments=self.args if args is None else args, **extra), ctx or self.ctx, caller)

    def pending(self, store=None):
        lever = self.m.tool_message(self.service.cockpit, self.args, self.m.Call("alpha-11", transport="http"))
        return (store or self.store).create("atc_message", self.args, lever.summary,
                                            "alpha-11", "test-client", lever.target)

    def refresh_needs(self, service=None):
        service = service or self.service
        model = service.model
        model["needs"] = self.m.needs_queue(model["groups"], [r.view(time.time()) for r in service.remotes],
                                           service.approvals.needs())
        model["counts"].update(self.m.needs_counts(model["needs"]))

    @staticmethod
    def text(result):
        return result["content"][0]["text"]


class StoreAndMCP(ApprovalCase):
    def test_all_three_unaskable_paths_create_pending_approvals(self):
        cases = [("unattended", "stdio", False, True, {"elicitation": {}}),
                 ("no elicitation", "stdio", True, True, {}),
                 ("handshake HTTP", "http", True, False, {"elicitation": {}})]
        with mock.patch.object(self.m, "deliver") as deliver:
            for label, transport, attended, modern, caps in cases:
                with self.subTest(path=label):
                    server = self.m.McpServer(self.service.cockpit, transport, attended=attended)
                    result = self.call(server=server, ctx=dict(self.ctx, modern=modern, caps=caps))
                    row = self.store.access()[-1]

                    self.assertFalse(result["isError"], result)
                    self.assertIn("Waiting for approval " + row["id"], self.text(result))
                    self.assertIn('approval: "' + row["id"] + '"', self.text(result))
                    self.assertEqual((row["state"], row["caller"], row["client"]), ("pending", "alpha-11", "test-client"))
                    self.assertEqual(row["expires"] - row["created"], 600)
            deliver.assert_not_called()
        self.assertEqual(len(self.store.access()), 3)
        self.assertEqual(os.stat(self.store.path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.join(self.store.directory, "approvals.lock")).st_mode & 0o777, 0o600)
        with open(self.m.AUDIT_LOG) as fh:
            self.assertEqual(sum(json.loads(line)["outcome"] == "pending" for line in fh), 3)

    def test_wait_then_approve_runs_once_and_recall_is_refused(self):
        ready = threading.Event()
        original = self.store.create
        def create(*args):
            row = original(*args)
            ready.set()
            return row
        with mock.patch.dict(os.environ, {"ATC_APPROVAL_WAIT": "2"}), mock.patch.object(self.m, "APPROVAL_POLL", 0.01):
            with mock.patch.object(self.store, "create", side_effect=create):
                with mock.patch.object(self.m, "deliver", return_value=(True, "sent")) as deliver:
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        future = pool.submit(self.call)
                        self.assertTrue(ready.wait(1))
                        row = self.store.access()[0]
                        self.store.decide(row["id"], "approve", "owner", "terminal")
                        result = future.result(timeout=2)
                    replay = self.call(dict(self.args, approval=row["id"]))

                    self.assertFalse(result["isError"])
                    self.assertTrue(replay["isError"])
                    self.assertIn("used", self.text(replay))
                    deliver.assert_called_once()
        self.assertEqual(self.store.get(row["id"])["state"], "used")

    def test_recall_after_wait_runs_once_across_instances_and_concurrent_requests(self):
        self.call()
        row = self.store.access()[0]
        self.m.Approvals(self.store.directory).decide(row["id"], "approve", "owner", "terminal")
        server = self.m.McpServer(self.service.cockpit, "http")
        with mock.patch.object(self.m, "deliver", return_value=(True, "sent")) as deliver:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: self.call(dict(self.args, approval=row["id"]), server=server), range(8)))

            self.assertEqual(sum(not r["isError"] for r in results), 1)
            deliver.assert_called_once()
        self.assertEqual(self.store.get(row["id"])["state"], "used")

    def test_store_claim_is_atomic_between_processes(self):
        row = self.pending()
        self.store.decide(row["id"], "approve", "owner", "terminal")
        script = """import runpy, sys
m = runpy.run_path(sys.argv[1])
try:
    m['Approvals'](sys.argv[2]).consume(sys.argv[3], 'atc_message',
        {'session': 'beta-22', 'text': 'the private message'}, 'alpha-11', sys.argv[4])
except m['WebError']:
    sys.exit(1)
"""
        procs = [subprocess.Popen([sys.executable, "-c", script, fixture.ATC, self.store.directory, row["id"], row["summary"]],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
        for proc in procs:
            out, err = proc.communicate(timeout=10)
            self.assertEqual((out, err), (b"", b""))
        self.assertEqual(sorted(proc.returncode for proc in procs), [0, 1, 1, 1])

    def test_pending_denied_expired_and_invalid_ids_refuse(self):
        with mock.patch.object(self.m, "deliver") as deliver:
            for state in ("pending", "denied", "expired"):
                row = self.pending()
                if state == "denied":
                    self.store.decide(row["id"], "deny", "owner", "terminal")
                elif state == "expired":
                    self.store.decide(row["id"], "approve", "owner", "terminal")
                    self.store.access(lambda rows: rows[-1].update(expires=time.time() - 1))
                result = self.call(dict(self.args, approval=row["id"]))
                self.assertTrue(result["isError"])
                self.assertIn(state, self.text(result))
            for ident in (None, "", "../approvals", "X" * 16, [], 123):
                self.assertTrue(self.call(dict(self.args, approval=ident))["isError"])
            deliver.assert_not_called()

    def test_argument_caller_tool_mismatch_never_spends_approved_grant(self):
        row = self.pending()
        self.store.decide(row["id"], "approve", "owner", "terminal")
        args = dict(self.args, approval=row["id"])
        attempts = [(dict(args, text="different"), "alpha-11", "atc_message"),
                    (dict(args, allow_self=True), "alpha-11", "atc_message"),
                    (dict(args, dry_run=True), "alpha-11", "atc_message"),
                    (args, "other-caller", "atc_message"), (args, "alpha-11", "atc_interrupt")]
        with mock.patch.object(self.m, "deliver") as deliver, mock.patch.object(self.m, "interrupt_session") as interrupt:
            for arguments, caller, tool in attempts:
                result = self.call(arguments, caller=caller, name=tool)
                self.assertTrue(result["isError"])
                self.assertIn("does not match", self.text(result))
                self.assertEqual(self.store.get(row["id"])["state"], "approved")
            deliver.assert_not_called()
            interrupt.assert_not_called()

    def test_new_task_approval_cannot_switch_default_folder_between_transports(self):
        project = os.path.join(self.tmp.name, "project")
        os.mkdir(project)
        args = {"task": "write tests", "pool": "claude"}
        stdio = self.m.McpServer(self.service.cockpit, "stdio")
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": project}):
            self.call(args, server=stdio, name="atc_new_task")
            row = self.store.access()[0]
            self.store.decide(row["id"], "approve", "owner", "terminal")
            with mock.patch.object(self.m, "open_tab", return_value="opened") as opened:
                refused = self.call(dict(args, approval=row["id"]), name="atc_new_task")

                self.assertTrue(refused["isError"])
                self.assertIn("new approval", self.text(refused))
                self.assertIn("without approval", self.text(refused))
                self.assertEqual(self.store.get(row["id"])["state"], "approved")
                opened.assert_not_called()
                # The original resolved context can still use the matching grant, once.
                accepted = self.call(dict(args, approval=row["id"]), server=stdio, name="atc_new_task")
                self.assertFalse(accepted["isError"], accepted)
                self.assertEqual(opened.call_args[0][0], project)
                self.assertEqual(self.store.get(row["id"])["state"], "used")
                self.assertTrue(self.call(dict(args, approval=row["id"]), server=stdio, name="atc_new_task")["isError"])
                opened.assert_called_once()

    def test_same_arguments_cannot_retarget_a_named_session_or_changed_folder(self):
        args = dict(self.args, session="beta")
        original = dict(self.service.model["groups"]["waiting"][0])
        for change in ({"cwd": "/workspace/different"}, {"sid": "another-session"}):
            with self.subTest(change=change):
                self.service.model["groups"]["waiting"][0] = dict(original)
                self.call(args)
                row = self.store.access()[-1]
                self.store.decide(row["id"], "approve", "owner", "terminal")
                self.service.model["groups"]["waiting"][0].update(change)
                with mock.patch.object(self.m, "deliver") as deliver:
                    result = self.call(dict(args, approval=row["id"]))

                    self.assertTrue(result["isError"])
                    self.assertIn("resolved action", self.text(result))
                    self.assertIn("new approval", self.text(result))
                    deliver.assert_not_called()
                self.assertEqual(self.store.get(row["id"])["state"], "approved")

    def test_waiter_rebuilds_and_refuses_a_changed_default_folder(self):
        project, other = (os.path.join(self.tmp.name, name) for name in ("project", "other"))
        for folder in (project, other):
            os.mkdir(folder)
        server = self.m.McpServer(self.service.cockpit, "stdio")
        args = {"task": "write tests", "pool": "claude"}
        def approve_in_changed_context(row, _args):
            os.environ["CLAUDE_PROJECT_DIR"] = other
            self.store.decide(row["id"], "approve", "owner", "terminal")
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": project}):
            with mock.patch.object(self.m, "notify_approval", side_effect=approve_in_changed_context):
                with mock.patch.object(self.m, "open_tab") as opened:
                    result = self.call(args, server=server, name="atc_new_task")

                    self.assertTrue(result["isError"])
                    self.assertIn("new approval", self.text(result))
                    opened.assert_not_called()
        self.assertEqual(self.store.access()[0]["state"], "approved")

    def test_waiter_executes_the_rebuilt_lever_when_summary_still_matches(self):
        original = self.service.model["groups"]["waiting"][0]
        current = dict(original)
        def approve(row, _args):
            self.service.model["groups"]["waiting"][0] = current
            self.store.decide(row["id"], "approve", "owner", "terminal")
        with mock.patch.object(self.m, "notify_approval", side_effect=approve):
            with mock.patch.object(self.m, "deliver", return_value=(True, "sent")) as deliver:
                result = self.call()

                self.assertFalse(result["isError"])
                deliver.assert_called_once()
                self.assertIs(deliver.call_args[0][2], current)
                self.assertIsNot(deliver.call_args[0][2], original)
        self.assertEqual(self.store.access()[0]["state"], "used")

    def test_approval_wait_defaults_validates_and_honours_zero_and_fractional_seconds(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("ATC_APPROVAL_WAIT", None)
            self.assertEqual(self.m.approval_wait(), 45)
        for value in ("-1", "nan", "inf", "-inf", "", "invalid"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"ATC_APPROVAL_WAIT": value}):
                result = self.call()
                self.assertTrue(result["isError"])
                self.assertIn("ATC_APPROVAL_WAIT", self.text(result))
                self.assertEqual(self.store.access(), [])
        with mock.patch.object(self.m.time, "sleep", side_effect=AssertionError("zero must not sleep")):
            self.assertFalse(self.call()["isError"])
        with mock.patch.dict(os.environ, {"ATC_APPROVAL_WAIT": "0.03"}):
            start = time.monotonic()
            result = self.call()
            elapsed = time.monotonic() - start
        self.assertFalse(result["isError"])
        self.assertGreaterEqual(elapsed, 0.03)
        self.assertLess(elapsed, 2)
        self.assertEqual(len(self.store.access()), 2)

    def test_failed_lever_still_spends_grant(self):
        row = self.pending()
        self.store.decide(row["id"], "approve", "owner", "terminal")
        with mock.patch.object(self.m, "deliver", return_value=(False, "blocked on a prompt")) as deliver:
            result = self.call(dict(self.args, approval=row["id"]))
            replay = self.call(dict(self.args, approval=row["id"]))

            self.assertTrue(result["isError"])
            self.assertTrue(replay["isError"])
            deliver.assert_called_once()
        self.assertEqual(self.store.get(row["id"])["state"], "used")

    def test_wait_then_deny_refuses(self):
        def deny(row, _args):
            self.store.decide(row["id"], "deny", "owner", "terminal")
        with mock.patch.object(self.m, "notify_approval", side_effect=deny):
            with mock.patch.object(self.m, "deliver") as deliver:
                result = self.call()
                self.assertTrue(result["isError"])
                self.assertIn("denied", self.text(result))
                deliver.assert_not_called()

    def test_attended_mrtr_handshake_policy_dry_run_and_early_refusals_unchanged(self):
        ctx = dict(self.ctx, caps={"elicitation": {}})
        question = self.call(ctx=ctx)
        self.assertEqual(question["resultType"], "input_required")
        with mock.patch.object(self.m, "deliver", return_value=(True, "sent")) as deliver:
            result = self.call(ctx=ctx, requestState=question["requestState"],
                               inputResponses={"confirm": {"action": "accept"}})
            self.assertFalse(result["isError"])
            deliver.assert_called_once()
            self.assertEqual(self.call(ctx=ctx, requestState=question["requestState"],
                                      inputResponses={"confirm": {"action": "accept"}})["resultType"], "input_required")
        asked = mock.Mock(return_value={"action": "decline"})
        legacy = self.m.McpServer(self.service.cockpit, "stdio", ask=asked)
        self.assertTrue(self.call(server=legacy, ctx=dict(ctx, modern=False, version="2025-11-25"))["isError"])
        asked.assert_called_once()
        for policy in ("auto", "off"):
            with mock.patch.dict(os.environ, {"ATC_MCP_LEVERS": policy}):
                server = self.m.McpServer(self.service.cockpit, "stdio")
                with mock.patch.object(self.m, "deliver", return_value=(True, "sent")) as deliver:
                    self.assertEqual(self.call(server=server)["isError"], policy == "off")
                    self.assertEqual(deliver.call_count, int(policy == "auto"))
        with mock.patch.object(self.m, "deliver", return_value=(True, "dry run")):
            self.assertFalse(self.call(dict(self.args, dry_run=True))["isError"])
        self.assertTrue(self.call({"session": "missing", "text": "x"})["isError"])
        self.assertTrue(self.call(caller="beta-22")["isError"])
        self.service.model["groups"]["waiting"][0]["group"] = "blocked"
        self.assertTrue(self.call()["isError"])
        self.assertEqual(self.store.access(), [])

    def test_notify_uses_argv_one_line_and_never_message_text(self):
        row = self.pending()
        command = "notify 'owner device'"
        with mock.patch.dict(os.environ, {"ATC_NOTIFY_CMD": command}):
            with mock.patch.object(self.m.subprocess, "run") as run:
                self.m.notify_approval(row, self.args)
                args, kwargs = run.call_args
                self.assertEqual(args[0], ["notify", "owner device", "atc: approval waiting: atc_message on beta-22 (" + row["id"] + ")"])
                self.assertNotIn(self.args["text"], str(args))
                self.assertFalse(kwargs.get("shell", False))
                self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            for error in (OSError("no notify"), subprocess.TimeoutExpired("notify", 5)):
                with mock.patch.object(self.m.subprocess, "run", side_effect=error):
                    self.m.notify_approval(row, self.args)
        with mock.patch.object(self.m, "notify_approval") as notify:
            self.call()
            notify.assert_called_once()

    def test_pruning_permissions_and_missing_store(self):
        self.assertEqual(self.store.access(), [])
        self.assertFalse(os.path.exists(self.store.directory))
        row = self.pending()
        self.store.access(lambda rows: rows[0].update(expires=time.time() - 1))
        self.assertEqual(self.store.get(row["id"])["state"], "expired")
        self.assertEqual(self.store.needs(), [])
        self.store.access(lambda rows: rows[0].update(expires=time.time() - 601))
        self.assertEqual(self.store.access(), [])
        os.chmod(self.store.path, 0o644)
        self.assertTrue(self.call()["isError"])
        self.assertEqual(os.stat(self.store.path).st_mode & 0o777, 0o644)

    def test_all_levers_document_approval_and_mcp_cannot_decide(self):
        for name, tool in self.m.TOOLS.items():
            self.assertEqual("approval" in tool["spec"]["inputSchema"]["properties"], tool["lever"], name)
        for name in ("atc_approve", "atc_deny", "approve", "deny"):
            with self.assertRaises(self.m.BadParams):
                self.call(name=name)
        self.assertEqual(len(self.m.TOOLS), 20)

    def test_cli_listing_tty_rule_and_real_subprocess_refusal(self):
        row = self.pending()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(self.m.approval_cli(["approvals"], self.store), 0)
        self.assertIn(row["id"], output.getvalue())
        self.assertIn("alpha-11", output.getvalue())
        for command in ("approve", "deny"):
            with self.assertRaisesRegex(self.m.WebError, "TTY"):
                self.m.approval_cli([command, row["id"]], self.store, is_tty=lambda: False)
            proc = subprocess.run([sys.executable, fixture.ATC, command, row["id"]],
                                  input="", capture_output=True, text=True, timeout=10)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("TTY", proc.stderr)
            self.assertEqual(self.store.get(row["id"])["state"], "pending")
        with contextlib.redirect_stdout(io.StringIO()):
            self.m.approval_cli(["approve", row["id"]], self.store, is_tty=lambda: True)
        self.assertEqual(self.store.get(row["id"])["state"], "approved")
        second = self.pending()
        with contextlib.redirect_stdout(io.StringIO()):
            self.m.approval_cli(["deny", second["id"]], self.store, is_tty=lambda: True)
        self.assertEqual(self.store.get(second["id"])["state"], "denied")

    def test_real_terminal_cli_decides_and_notification_program_receives_only_metadata(self):
        row = self.pending()
        master, slave = pty.openpty()
        try:
            proc = subprocess.run([sys.executable, fixture.ATC, "approve", row["id"]], stdin=slave,
                                  capture_output=True, text=True, timeout=10)
        finally:
            os.close(slave)
            os.close(master)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.store.get(row["id"])["state"], "approved")
        output = os.path.join(self.tmp.name, "notice.json")
        script = os.path.join(self.tmp.name, "notify.py")
        with open(script, "w") as fh:
            fh.write("import json, sys\nwith open(sys.argv[1], 'w') as f: json.dump(sys.argv[2:], f)\n")
        with mock.patch.dict(os.environ, {"ATC_NOTIFY_CMD": self.m.shlex.join([sys.executable, script, output])}):
            self.call()
        with open(output) as fh:
            notice = json.load(fh)
        self.assertEqual(len(notice), 1)
        self.assertIn("atc: approval waiting: atc_message on beta-22", notice[0])
        self.assertNotIn(self.args["text"], notice[0])

    def test_screen_reads_cached_approvals_and_never_takes_the_store_lock(self):
        row = self.pending()
        self.store.cached = self.store.access()
        self.store.background = True
        with mock.patch.object(self.store, "access", side_effect=AssertionError("screen must not read disk")):
            model = self.service.fleet.model(time.time())
        self.assertEqual(model["needs"][0]["id"], row["id"])

    def test_tui_two_press_approve_deny_dry_run_and_needs_counts(self):
        for key, expected in (("y", "approved"), ("n", "denied")):
            row = self.pending()
            model = self.service.fleet.model(time.time())
            self.assertEqual(model["needs"][0]["kind"], "approval")
            self.assertEqual(model["counts"]["needs_urgent"], 1)
            lines = self.m.fleet_lines(model, 140, True)
            selected = "approval::" + row["id"]
            self.assertEqual(next(k for _, k in lines if k), selected)
            app = self.m.App(self.service.fleet, None, None, None, ["claude"], False)
            app.selectable, app.ui["selected"] = [selected], selected
            app.handle(key, model)
            self.assertEqual(self.store.get(row["id"])["state"], "pending")
            self.assertIn("press " + key + " again", app.flash[0])
            app.handle(key, model)
            deadline = time.monotonic() + 2
            while self.store.get(row["id"])["state"] == "pending" and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(self.store.get(row["id"])["state"], expected)
            self.assertEqual(app.mode, "normal")
        row = self.pending()
        model = self.service.fleet.model(time.time())
        app.dry_run = True
        app.selectable = ["approval::" + row["id"]]
        app.ui["selected"] = app.selectable[0]
        app.handle("y", model)
        app.handle("y", model)
        self.assertEqual(self.store.get(row["id"])["state"], "pending")
        self.assertIn("dry run", app.flash[0])

    def test_approval_detail_is_wrapped_scrollable_and_confirms_the_target_first(self):
        self.args["text"] = "An extended message that pushes the target beyond the first screen row. " * 8
        row = self.pending()
        model = self.service.fleet.model(time.time())
        model["machine"] = "laptop"
        app = self.m.App(self.service.fleet, None, None, None, ["claude"], False)
        app.ui["selected"] = "approval::" + row["id"]
        item = self.m.resolve(self.service.fleet, model, app.ui["selected"])
        self.assertEqual(self.m.selection_name(item), "Approval " + row["id"])
        lines = self.m.selection_lines(self.service.fleet, model, item, 0, 60)
        action = " ".join(self.m.plain(line) for line in lines if line[0][1] == "plain")
        self.assertEqual(action, "Action: " + self.m.clean(row["summary"]))
        self.assertTrue(all(len(self.m.plain(line)) <= 60 for line in lines))

        for key in ("\n", "f"):
            app.handle(key, model)
            self.assertEqual(app.ui["view"], "detail")
            frames = []
            while True:
                before = app.ui["view_scroll"]
                frame = self.m.render_view(self.service.fleet, model, app.ui, 60, 12)
                frames.extend(self.m.plain(line) for line in frame)
                if before > app.ui["view_scroll"]:
                    break  # reached the last page; render_view clamps scroll
                app.handle(self.m.curses.KEY_NPAGE, model)
            visible = "\n".join(frames)
            for label in ("Tool: atc_message", "Machine: laptop", "Caller: alpha-11", "Client: test-client",
                          "Expires:", "Target: beta", "y y approves", "/workspace/example"):
                self.assertIn(label, visible)
            self.assertGreater(app.ui["view_scroll"], 0)
            app.handle(self.m.curses.KEY_PPAGE, model)
            self.assertEqual(app.ui["view_scroll"], 0)
            app.handle(self.m.curses.KEY_DOWN, model)
            self.assertEqual(app.ui["view_scroll"], 1)
            app.handle(self.m.curses.KEY_UP, model)
            self.assertEqual(app.ui["view_scroll"], 0)
            app.handle("\x1b", model)
            self.assertIsNone(app.ui["view"])
        app.handle("f", model)
        app.handle("y", model)
        footer = self.m.plain(self.m.fit(app.footer(time.time()), 80))
        self.assertIn("approve beta", footer)
        self.assertNotIn("extended message", footer)
        self.assertEqual(self.store.get(row["id"])["state"], "pending")
        app.handle("y", model)
        deadline = time.monotonic() + 2
        while self.store.get(row["id"])["state"] == "pending" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.store.get(row["id"])["state"], "approved")

    def test_approval_detail_denial_still_needs_two_presses(self):
        row = self.pending()
        model = self.service.fleet.model(time.time())
        app = self.m.App(self.service.fleet, None, None, None, ["claude"], False)
        app.ui["selected"] = "approval::" + row["id"]
        app.handle("f", model)
        app.handle("n", model)
        self.assertIn("deny beta", app.flash[0])
        self.assertEqual(self.store.get(row["id"])["state"], "pending")
        app.handle("n", model)
        deadline = time.monotonic() + 2
        while self.store.get(row["id"])["state"] == "pending" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.store.get(row["id"])["state"], "denied")
        self.assertEqual(app.mode, "normal")

    def test_needs_fleet_json_and_remote_metadata(self):
        row = self.pending()
        self.refresh_needs()
        status, data = self.service.get("/api/fleet")
        self.assertEqual(status, 200)
        approval = data["needs"][0]
        self.assertEqual((approval["kind"], approval["id"]), ("approval", row["id"]))
        remote = {"host": "laptop", "groups": {}, "ok": True, "needs": data["needs"]}
        combined = self.m.needs_queue({}, [remote])
        self.assertEqual(combined[0], dict(approval, host="laptop"))
        self.assertEqual(data["counts"]["needs_urgent"], 1)
        proc = subprocess.run([sys.executable, fixture.ATC, "--json", "--no-hosts"], capture_output=True,
                              text=True, timeout=20, env=dict(os.environ, ATC_CLAUDE="", ATC_AMAIL="",
                                                            ATC_HERDR="", ATC_QUOTAMAX=""))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        printed = json.loads(proc.stdout)
        self.assertEqual(printed["needs"][0]["id"], row["id"])
        self.assertEqual(printed["counts"]["needs_urgent"], 1)
        self.assertIn(row["id"], self.m.tool_needs_me(self.service.cockpit, {}, self.m.Call()))


class WebApprovals(ApprovalCase, HTTP):
    def setUp(self):
        super().setUp()
        if not OPENSSL:
            self.skipTest("openssl is needed for real passkey signatures")
        self.service = self.make_service(web=True)
        self.url = self.start(self.service)
        self.key, self.cookie = self.enroll()

    def options(self, row, host="local"):
        status, data, _ = self.request("/api/web/approvals/" + row["id"] + "/options", {"host": host}, self.cookie)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["userVerification"], "required")
        return data

    def approve(self, row, options, host="local", cookie=None, **kwargs):
        body = self.key.response(options["challenge"], **kwargs)
        return self.request("/api/web/approvals/" + row["id"] + "/approve", dict(body, host=host), cookie or self.cookie)

    def test_session_or_bearer_cannot_approve_but_fresh_assertion_can(self):
        row = self.pending()
        base = "/api/web/approvals/" + row["id"]
        self.assertEqual(self.request(base + "/approve", {}, self.cookie)[0], 400)
        for stage in ("options", "approve", "deny"):
            for token in (TOKEN, PEER):
                self.assertEqual(self.request(base + "/" + stage, {}, headers={"Authorization": "Bearer " + token})[0], 401)
        for headers in ({"Origin": None}, {"Origin": "https://wrong.example.com"}, {"X-Atc-Web": None}):
            self.assertEqual(self.request(base + "/deny", {}, self.cookie, headers)[0], 403)
        self.assertEqual(self.store.get(row["id"])["state"], "pending")
        options = self.options(row)
        sessions = dict(self.service.web.sessions)

        status, result, _ = self.approve(row, options)

        self.assertEqual(status, 200, result)
        self.assertTrue(result["ok"])
        self.assertEqual(self.service.web.sessions, sessions)
        saved = self.store.get(row["id"])
        self.assertEqual((saved["state"], saved["decided_by"], saved["via"]), ("approved", "Phone", "web passkey"))
        self.assertEqual(self.approve(row, options)[0], 409)
        with mock.patch.object(self.m, "deliver", return_value=(True, "sent")) as deliver:
            self.assertFalse(self.call(dict(self.args, approval=row["id"]))["isError"])
            self.assertTrue(self.call(dict(self.args, approval=row["id"]))["isError"])
            deliver.assert_called_once()

    def test_deny_only_needs_session(self):
        row = self.pending()
        status, result, _ = self.request("/api/web/approvals/" + row["id"] + "/deny", {}, self.cookie)

        self.assertEqual(status, 200, result)
        saved = self.store.get(row["id"])
        self.assertEqual((saved["state"], saved["via"]), ("denied", "web session"))
        self.assertTrue(self.call(dict(self.args, approval=row["id"]))["isError"])

    def test_assertion_is_bound_to_id_digest_session_and_not_a_login_challenge(self):
        for fault in ("id", "digest", "session", "login", "uv", "signature", "expired"):
            with self.subTest(fault=fault):
                row = self.pending()
                options = self.options(row)
                target, cookie = row, self.cookie
                kwargs = {}
                if fault == "id":
                    target = self.pending()
                elif fault == "digest":
                    self.store.access(lambda rows: rows[-1].update(args_digest="changed"))
                elif fault == "session":
                    cookie = self.enroll()[1]
                elif fault == "login":
                    options = self.begin("login")
                elif fault == "uv":
                    kwargs["flags"] = 1
                elif fault == "signature":
                    kwargs["origin"] = "https://wrong.example.com"
                elif fault == "expired":
                    self.service.web.challenges[options["challenge"]]["expires"] = time.time() - 1
                status, _, _ = self.approve(target, options, cookie=cookie, **kwargs)
                self.assertIn(status, (400, 401))
                self.assertEqual(self.store.get(row["id"])["state"], "pending")
                self.assertIn(self.approve(row, options)[0], (400, 401))

    def test_expiry_or_decision_while_tapping_cannot_approve(self):
        for state in ("denied", "expired"):
            row = self.pending()
            options = self.options(row)
            if state == "denied":
                self.store.decide(row["id"], "deny", "owner", "terminal")
            else:
                self.store.access(lambda rows: rows[-1].update(expires=time.time() - 1))
            self.assertEqual(self.approve(row, options)[0], 409)
            self.assertEqual(self.store.get(row["id"])["state"], state)

    def test_hub_decides_owning_service_over_peer_with_owner_audit(self):
        leaf = self.make_service("laptop")
        leaf_url = self.start(leaf)
        remote = self.m.RemoteAtc(leaf_url, TOKEN, background=False)
        self.service.remotes.append(remote)
        for decision in ("approve", "deny"):
            self.call(server=leaf.mcp)
            row = leaf.approvals.access()[-1]
            self.refresh_needs(leaf)
            remote.fetch()
            remote.host = "laptop"
            self.refresh_needs()
            self.assertEqual(self.service.model["needs"][0]["host"], "laptop")
            self.assertEqual(self.service.model["needs"][0]["id"], row["id"])
            self.assertEqual(self.store.access(), [])
            base = "/api/peer/approvals/" + row["id"]
            body = {"decision": decision, "by": {"name": "Phone", "hub": "hub"}}
            self.assertEqual(self.request(base, body, headers={"Authorization": "Bearer " + TOKEN}, url=leaf_url)[0], 401)
            with mock.patch.object(self.m, "remote_tool", side_effect=AssertionError("must not use SSH")):
                if decision == "approve":
                    opts = self.options(row, host="laptop")
                    status, result, _ = self.approve(row, opts, host="laptop")
                else:
                    status, result, _ = self.request("/api/web/approvals/" + row["id"] + "/deny", {"host": "laptop"}, self.cookie)
            self.assertEqual(status, 200, result)
            self.assertTrue(result["ok"])
            saved = leaf.approvals.get(row["id"])
            self.assertEqual(saved["state"], "approved" if decision == "approve" else "denied")
            self.assertEqual(saved["via"], "web passkey via hub" if decision == "approve" else "web session via hub")
            if decision == "approve":
                with mock.patch.object(self.m, "deliver", return_value=(True, "sent")) as deliver:
                    self.assertFalse(self.call(dict(self.args, approval=row["id"]), server=leaf.mcp)["isError"])
                    self.assertTrue(self.call(dict(self.args, approval=row["id"]), server=leaf.mcp)["isError"])
                    deliver.assert_called_once()
                    self.assertIs(deliver.call_args[0][0], leaf.fleet)
        with open(self.m.AUDIT_LOG) as fh:
            rows = [json.loads(line) for line in fh]
        self.assertTrue(any(r.get("via") == "web passkey via hub" and r["outcome"] == "approved" for r in rows))

    def test_remote_assertion_cannot_be_used_on_another_machine(self):
        leaf = self.make_service("laptop")
        remote = self.m.RemoteAtc(self.start(leaf), TOKEN, background=False)
        row = self.pending(leaf.approvals)
        self.refresh_needs(leaf)
        remote.fetch()
        remote.host = "laptop"
        self.service.remotes.append(remote)
        opts = self.options(row, "laptop")
        # Even a same-id, same-digest local entry cannot use a challenge for the laptop.
        self.store.access(lambda rows: rows.append(dict(row)))
        status, _, _ = self.approve(row, opts, "local")
        self.assertEqual(status, 400)
        self.assertEqual(self.store.get(row["id"])["state"], "pending")
        self.assertEqual(leaf.approvals.get(row["id"])["state"], "pending")

    def test_http_mcp_handshake_and_no_elicitation_return_pending_and_retry_once(self):
        for modern in (False, True):
            params = {"name": "atc_message", "arguments": self.args}
            headers = {"Authorization": "Bearer " + TOKEN, "Origin": None, "X-Atc-Caller": "alpha-11"}
            if modern:
                params["_meta"] = {self.m.MCP_META + "protocolVersion": "2026-07-28",
                                   self.m.MCP_META + "clientCapabilities": {}}
                headers.update({"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/call", "Mcp-Name": "atc_message"})
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
            status, reply, _ = self.request("/mcp", body, headers=headers)
            self.assertEqual(status, 200, reply)
            self.assertFalse(reply["result"]["isError"])
            row = self.store.access()[-1]
            self.assertIn(row["id"], self.text(reply["result"]))
            self.assertEqual(self.approve(row, self.options(row))[0], 200)
            params["arguments"] = dict(self.args, approval=row["id"])
            with mock.patch.object(self.m, "deliver", return_value=(True, "sent")) as deliver:
                done = self.request("/mcp", body, headers=headers)[1]["result"]
                replay = self.request("/mcp", body, headers=headers)[1]["result"]
                self.assertFalse(done["isError"])
                self.assertTrue(replay["isError"])
                deliver.assert_called_once()

    def test_http_waiter_runs_after_web_passkey_without_recall(self):
        headers = {"Authorization": "Bearer " + TOKEN, "Origin": None, "X-Atc-Caller": "alpha-11"}
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "atc_message", "arguments": self.args}}
        with mock.patch.dict(os.environ, {"ATC_APPROVAL_WAIT": "3"}), mock.patch.object(self.m, "APPROVAL_POLL", 0.01):
            with mock.patch.object(self.m, "deliver", return_value=(True, "sent")) as deliver:
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    waiting = pool.submit(self.request, "/mcp", body, headers=headers)
                    deadline = time.monotonic() + 2
                    while not self.store.access() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    row = self.store.access()[0]
                    self.assertEqual(self.approve(row, self.options(row))[0], 200)
                    status, reply, _ = waiting.result(timeout=3)
                self.assertEqual(status, 200, reply)
                self.assertFalse(reply["result"]["isError"])
                deliver.assert_called_once()
        self.assertEqual(self.store.get(row["id"])["state"], "used")

    def test_peer_lost_response_and_stale_offline_grants_never_retry(self):
        leaf = self.make_service("laptop")
        remote = self.m.RemoteAtc(self.start(leaf), TOKEN, background=False)
        row = self.pending(leaf.approvals)
        self.refresh_needs(leaf)
        remote.fetch()
        remote.host = "laptop"
        self.service.remotes.append(remote)
        options = self.options(row, "laptop")
        body = dict(self.key.response(options["challenge"]), host="laptop")
        identity = self.service.web.identity(self.cookie)
        with mock.patch.object(self.m.urllib.request, "urlopen", side_effect=OSError("offline")) as request:
            with self.assertRaisesRegex(self.m.WebError, "may have arrived"):
                self.service.web_approval(row["id"], "approve", body, identity)
            request.assert_called_once()
            with self.assertRaisesRegex(self.m.WebError, "reused"):
                self.service.web_approval(row["id"], "approve", body, identity)
            request.assert_called_once()
        self.assertEqual(leaf.approvals.get(row["id"])["state"], "pending")
        remote.error = "offline"
        self.assertEqual(self.request("/api/web/approvals/" + row["id"] + "/options", {"host": "laptop"}, self.cookie)[0], 503)

    def test_peer_disabled_read_only_and_invalid_payloads(self):
        row = self.pending()
        path = "/api/peer/approvals/" + row["id"]
        auth = {"Authorization": "Bearer " + PEER}
        body = {"decision": "approve", "by": {"name": "Phone", "hub": "hub"}}
        for bad in ({}, {"decision": "approve", "by": "Phone"}, dict(body, decision="used")):
            self.assertEqual(self.request(path, bad, headers=auth)[0], 400)
        self.service.writes = False
        self.assertEqual(self.request(path, body, headers=auth)[0], 404)
        self.assertEqual(self.request("/api/web/approvals/" + row["id"] + "/deny", {}, self.cookie)[0], 403)
        self.service.writes = True
        self.service.peer_secret = None
        self.assertEqual(self.request(path, body, headers=auth)[0], 404)
        self.assertEqual(self.store.get(row["id"])["state"], "pending")


if __name__ == "__main__":
    unittest.main()
