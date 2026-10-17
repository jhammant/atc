"""Browser identity and HTTP boundaries, plus live pane and hub-to-peer checks against fixture.py.

The passkey vectors are built independently with OpenSSL, not a mock signature verifier.
"""
import base64
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import fixture
from test_units import load_atc

ORIGIN = "https://atc.example.com"
TOKEN = "test-machine-token"
PEER = "test-peer-secret"
OPENSSL = shutil.which("openssl")


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def cbor(value):
    def head(major, length):
        if length < 24:
            return bytes([major * 32 + length])
        width = 1 if length < 256 else 2 if length < 65536 else 4
        return bytes([major * 32 + {1: 24, 2: 25, 4: 26}[width]]) + length.to_bytes(width, "big")
    if isinstance(value, int):
        return head(0, value) if value >= 0 else head(1, -1 - value)
    if isinstance(value, bytes):
        return head(2, len(value)) + value
    if isinstance(value, str):
        raw = value.encode()
        return head(3, len(raw)) + raw
    if isinstance(value, dict):
        return head(5, len(value)) + b"".join(cbor(k) + cbor(v) for k, v in value.items())
    if isinstance(value, list):
        return head(4, len(value)) + b"".join(cbor(v) for v in value)
    raise AssertionError("unsupported test vector")


class Passkey:
    def __init__(self, directory):
        self.key = os.path.join(directory, "private-" + secrets.token_hex(6) + ".pem")
        subprocess.run([OPENSSL, "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", self.key],
                       check=True, capture_output=True)
        os.chmod(self.key, 0o600)
        public = subprocess.run([OPENSSL, "pkey", "-in", self.key, "-pubout", "-outform", "DER"],
                                check=True, capture_output=True).stdout
        assert public[-65] == 4
        self.cose = {1: 2, 3: -7, -1: 1, -2: public[-64:-32], -3: public[-32:]}
        self.cid = secrets.token_bytes(32)

    def response(self, challenge, register=False, count=0, flags=None, origin=ORIGIN, rp="atc.example.com"):
        client = json.dumps({"type": "webauthn.create" if register else "webauthn.get",
                             "challenge": challenge, "origin": origin, "crossOrigin": False}).encode()
        flags = (0x45 if register else 0x05) if flags is None else flags
        auth = hashlib.sha256(rp.encode()).digest() + bytes([flags]) + count.to_bytes(4, "big")
        response = {"clientDataJSON": b64(client)}
        if register:
            auth += bytes(16) + len(self.cid).to_bytes(2, "big") + self.cid + cbor(self.cose)
            response["attestationObject"] = b64(cbor({"fmt": "none", "attStmt": {}, "authData": auth}))
        else:
            signature = subprocess.run([OPENSSL, "dgst", "-sha256", "-sign", self.key],
                                       input=auth + hashlib.sha256(client).digest(), check=True, capture_output=True).stdout
            response.update(authenticatorData=b64(auth), signature=b64(signature))
        return {"challenge": challenge, "id": b64(self.cid), "rawId": b64(self.cid),
                "type": "public-key", "response": response}


class HTTP:
    def start(self, service):
        server = self.m.make_server(service, "127.0.0.1:0")
        threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:" + str(server.server_address[1])

    def request(self, path, body=None, cookie=None, headers=None, url=None, raw=None):
        sent = {"Origin": ORIGIN, "X-Atc-Web": "1", "Content-Type": "application/json"}
        if cookie:
            sent["Cookie"] = cookie
        sent.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request((url or self.url) + path, data=data,
                                     headers={k: v for k, v in sent.items() if v is not None})
        try:
            response = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            data = response.read()
            content = json.loads(data) if response.headers.get_content_type() == "application/json" else data.decode()
            return response.status, content, response.headers

    def begin(self, kind="register", cookie=None, key_name="Phone", code=None):
        if kind == "register" and not cookie and code is None:
            code = self.m.web_enroll(self.service.web.directory, is_tty=lambda: True)
        status, data, _ = self.request("/api/web/auth/" + kind + "/options",
                                       {"name": key_name, "code": code}, cookie=cookie)
        self.assertEqual(status, 200, data)
        return data

    def finish(self, key, options, register=False, cookie=None, **kwargs):
        body = key.response(options["challenge"], register, **kwargs)
        return self.request("/api/web/auth/" + ("register" if register else "login") + "/finish", body, cookie=cookie)

    def enroll(self, key=None):
        key = key or Passkey(self.tmp.name)
        options = self.begin()
        status, data, headers = self.finish(key, options, register=True)
        self.assertEqual(status, 200, data)
        return key, headers["Set-Cookie"].split(";", 1)[0]


class WebHTTP(HTTP, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="atc-web-test-")
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"HOME": self.tmp.name, "ATC_PEER_TOKEN": "", "ATC_MCP_LEVERS": "confirm"})
        env.start()
        self.addCleanup(env.stop)
        self.m = load_atc({})
        self.service = self.make_service()
        self.url = self.start(self.service)

    def make_service(self, origin=ORIGIN, writes=True, peer=PEER):
        m = self.m
        service = m.Service(m.Fleet(), m.Quota(None, background=False), [], m.Terminals(), writes=writes,
                            token=TOKEN, background=False, web_origin=origin, peer_secret=peer,
                            web_dir=os.path.join(self.tmp.name, "web"))
        service.model = {"now": time.time(), "machine": "hub", "groups": {g: [] for g in m.GROUP_ORDER},
                         "hosts": [], "swarms": [], "agents": [], "counts": {}, "comms": [], "closed": []}
        service.at = time.time()
        return service

    def need_openssl(self):
        if not OPENSSL:
            self.skipTest("openssl is needed to build and verify P-256 passkey vectors")

    def test_static_files_csp_and_no_traversal(self):
        for path, ctype in [("/", "text/html"), ("/app.js", "text/javascript"), ("/app.css", "text/css")]:
            with self.subTest(path=path):
                status, text, headers = self.request(path, headers={"X-Forwarded-For": "100.64.0.1"})
                self.assertEqual(status, 200)
                self.assertEqual(headers.get_content_type(), ctype)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
                self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
                self.assertTrue(text)
        for path in ["/../atc.py", "/web/index.html", "/%2e%2e/atc.py", "/app.js/", "/web/../app.js"]:
            self.assertEqual(self.request(path)[0], 404, path)
        js = self.request("/app.js")[1]
        self.assertNotIn("innerHTML", js)
        self.assertNotIn("outerHTML", js)

    def test_web_off_and_no_auth_with_bearer(self):
        off = self.start(self.make_service(origin=None, peer=""))
        self.assertIn("web UI is off", self.request("/", url=off)[1])
        self.assertEqual(self.request("/api/web/model", url=off)[0], 404)
        self.assertEqual(self.request("/api/web/auth/login/options", {}, url=off)[0], 404)
        for path, body in [("model", None), ("session/test", None), ("passkeys", None), ("tools/atc_cap", {"arguments": {"direction": "up"}})]:
            for headers in [{}, {"Authorization": "Bearer " + TOKEN}, {"Authorization": "Bearer " + PEER}]:
                self.assertEqual(self.request("/api/web/" + path, body, headers=headers)[0], 401)

    def test_bootstrap_code_tty_expiry_single_use_and_permissions(self):
        m, directory = self.m, self.service.web.directory
        with self.assertRaisesRegex(m.WebError, "TTY"):
            m.web_enroll(directory, is_tty=lambda: False)
        with mock.patch.object(m.sys.stdin, "isatty", return_value=False):
            with self.assertRaises(m.WebError):
                m.web_enroll(directory)
        code = m.web_enroll(directory, is_tty=lambda: True, now=time.time() - 601)
        with self.assertRaisesRegex(m.WebError, "expired"):
            self.service.web.consume_code(code)
        code = m.web_enroll(directory, is_tty=lambda: True)
        with open(os.path.join(directory, "enroll.json")) as fh:
            self.assertNotIn(code, fh.read())
        self.service.web.consume_code(code)
        with self.assertRaises(m.WebError):
            self.service.web.consume_code(code)
        self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)
        for name in os.listdir(directory):
            self.assertEqual(os.stat(os.path.join(directory, name)).st_mode & 0o777, 0o600)
        with open(m.AUDIT_LOG) as fh:
            self.assertNotIn(code, fh.read())

    def test_registration_options_require_code_and_csrf(self):
        path = "/api/web/auth/register/options"
        self.assertEqual(self.request(path, {"name": "Phone"})[0], 401)
        for headers in [{"Origin": None}, {"Origin": "https://wrong.example.com"}, {"X-Atc-Web": None}]:
            self.assertEqual(self.request(path, {"name": "Phone"}, headers=headers)[0], 403)
        code = self.m.web_enroll(self.service.web.directory, is_tty=lambda: True)
        opts = self.begin(code=code)
        self.assertEqual(self.request(path, {"name": "Phone", "code": code})[0], 401)
        self.assertEqual(opts["pubKeyCredParams"], [{"type": "public-key", "alg": -7}])
        self.assertEqual(opts["authenticatorSelection"]["userVerification"], "required")
        self.assertEqual(opts["authenticatorSelection"]["residentKey"], "preferred")
        self.assertEqual(opts["attestation"], "none")

    def test_full_registration_signin_cookie_csrf_signout_audit(self):
        self.need_openssl()
        key, cookie = self.enroll()
        self.assertEqual(self.request("/api/web/model", cookie=cookie)[0], 200)
        options = self.begin("login")
        self.assertEqual(options["userVerification"], "required")
        status, data, headers = self.finish(key, options, count=1)
        self.assertEqual(status, 200, data)
        for flag in ["HttpOnly", "Secure", "SameSite=Strict", "Path=/", "Max-Age=43200"]:
            self.assertIn(flag, headers["Set-Cookie"])
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        body = {"arguments": {"direction": "up", "dry_run": True}}
        for headers in [{"Origin": None}, {"Origin": "https://wrong.example.com"}, {"X-Atc-Web": None}]:
            self.assertEqual(self.request("/api/web/tools/atc_cap", body, cookie, headers)[0], 403)
        status, result, _ = self.request("/api/web/tools/atc_cap", body, cookie)
        self.assertEqual((status, result["ok"], result["text"]), (200, True, "swarm cap up"))
        _, _, headers = self.request("/api/web/signout", {}, cookie)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.request("/api/web/model", cookie=cookie)[0], 401)
        with open(self.m.AUDIT_LOG) as fh:
            audit = [json.loads(line) for line in fh]
        self.assertTrue(any(r["tool"] == "login" and r["caller"] == "Phone" for r in audit))
        self.assertTrue(any(r["tool"] == "atc_cap" and r["caller"] == "Phone" for r in audit))
        self.assertNotIn(cookie.split("=", 1)[1], json.dumps(audit))
        self.assertEqual(self.service.web.data["keys"][b64(key.cid)]["sign_count"], 1)

    def test_rejects_bad_registration_and_assertion_vectors(self):
        self.need_openssl()
        key, _ = self.enroll()
        for register in (False, True):
            for fault in ("origin", "challenge", "rp", "uv", "up", "type", "cross_origin", "id", "trailing"):
                with self.subTest(register=register, fault=fault):
                    current = Passkey(self.tmp.name) if register else key
                    options = self.begin("register" if register else "login")
                    kwargs = {"count": 1}
                    if fault == "origin":
                        kwargs["origin"] = "https://wrong.example.com"
                    if fault == "rp":
                        kwargs["rp"] = "wrong.example.com"
                    if fault == "uv":
                        kwargs["flags"] = 0x41 if register else 0x01
                    if fault == "up":
                        kwargs["flags"] = 0x44 if register else 0x04
                    body = current.response(options["challenge"], register, **kwargs)
                    response = body["response"]
                    if fault in ("challenge", "type", "cross_origin"):
                        client = json.loads(base64.urlsafe_b64decode(response["clientDataJSON"] + "=="))
                        client[{"challenge": "challenge", "type": "type", "cross_origin": "crossOrigin"}[fault]] = True if fault == "cross_origin" else "wrong"
                        response["clientDataJSON"] = b64(json.dumps(client).encode())
                    if fault == "id":
                        body["rawId"] = b64(b"wrong")
                    if fault == "trailing":
                        field = "attestationObject" if register else "authenticatorData"
                        response[field] = b64(base64.urlsafe_b64decode(response[field] + "==") + b"\x00")
                    path = "/api/web/auth/" + ("register" if register else "login") + "/finish"
                    self.assertIn(self.request(path, body)[0], (400, 401))
                    # Even failed attempts spend the challenge.
                    self.assertIn("reused", self.request(path, current.response(options["challenge"], register, count=1))[1]["text"])

    def test_bad_signature_replay_expiry_and_counter_regression(self):
        self.need_openssl()
        key, _ = self.enroll()
        opts = self.begin("login")
        body = key.response(opts["challenge"], count=1)
        body["response"]["signature"] = b64(b"invalid signature")
        self.assertEqual(self.request("/api/web/auth/login/finish", body)[0], 401)
        opts = self.begin("login")
        self.service.web.challenges[opts["challenge"]]["expires"] = time.time() - 1
        self.assertEqual(self.finish(key, opts, count=1)[0], 400)
        opts = self.begin("login")
        self.assertEqual(self.finish(key, opts, count=2)[0], 200)
        self.assertEqual(self.finish(key, opts, count=3)[0], 400)
        for count in (0, 1, 2):
            self.assertIn("count", self.finish(key, self.begin("login"), count=count)[1]["text"])
        self.assertEqual(self.finish(key, self.begin("login"), count=3)[0], 200)

    def test_zero_counter_authenticators_and_missing_openssl(self):
        self.need_openssl()
        key, _ = self.enroll()
        self.assertEqual(self.finish(key, self.begin("login"))[0], 200)
        opts = self.begin("login")
        body = key.response(opts["challenge"])
        with mock.patch.object(self.m.shutil, "which", return_value=None):
            status, result, _ = self.request("/api/web/auth/login/finish", body)
        self.assertEqual(status, 503)
        self.assertIn("openssl", result["text"])

    def test_add_remove_passkeys_revokes_sessions_and_requires_last_confirmation(self):
        self.need_openssl()
        first, cookie = self.enroll()
        key = Passkey(self.tmp.name)
        opts = self.begin(cookie=cookie, key_name="Laptop")
        status, data, headers = self.finish(key, opts, register=True, cookie=cookie)
        self.assertEqual(status, 200, data)
        second_cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(len(self.request("/api/web/passkeys", cookie=second_cookie)[1]["passkeys"]), 2)
        self.assertEqual(self.request("/api/web/passkeys/remove", {"id": b64(first.cid)}, second_cookie)[0], 200)
        self.assertEqual(self.request("/api/web/model", cookie=cookie)[0], 401)
        self.assertEqual(self.request("/api/web/passkeys/remove", {"id": b64(key.cid)}, second_cookie)[0], 409)
        self.assertEqual(self.request("/api/web/passkeys/remove", {"id": b64(key.cid), "confirm_last": True}, second_cookie)[0], 200)
        self.assertEqual(self.request("/api/web/model", cookie=second_cookie)[0], 401)

    def test_enrollment_bound_to_signed_in_session_and_session_expiry(self):
        self.need_openssl()
        _, cookie = self.enroll()
        second = Passkey(self.tmp.name)
        opts = self.begin(cookie=cookie)
        self.assertEqual(self.finish(second, opts, register=True)[0], 401)
        token = cookie.split("=", 1)[1]
        self.service.web.sessions[token]["expires"] = time.time() - 1
        self.assertEqual(self.request("/api/web/model", cookie=cookie)[0], 401)

    def test_cbor_bounds_and_invalid_public_key(self):
        for data in [b"", b"\xbf\xff", b"\xa2\x01\x02\x01\x03", b"\x5a\xff\xff\xff\xff", b"\x81" * 10 + b"\x00", b"\xa1\x80\x01", b"x" * 65537]:
            with self.subTest(data=data[:10]):
                with self.assertRaises(self.m.WebError):
                    self.m.cbor_read(data)
        with self.assertRaises(self.m.WebError):
            self.m.cose_pem({1: 2, 3: -257, -1: 1, -2: bytes(32), -3: bytes(32)})
        if OPENSSL:
            with self.assertRaises(self.m.WebError):
                self.m.cose_pem({1: 2, 3: -7, -1: 1, -2: bytes(32), -3: bytes(32)})

    def test_peer_auth_separate_token_and_read_only(self):
        path = "/api/peer/tools/atc_cap"
        body = {"arguments": {"direction": "up", "dry_run": True}}
        off = self.start(self.make_service(origin=None, peer=""))
        self.assertEqual(self.request(path, body, url=off)[0], 404)
        read = self.start(self.make_service(origin=None, writes=False))
        self.assertEqual(self.request(path, body, url=read)[0], 404)
        for token in ["wrong", TOKEN, ""]:
            self.assertEqual(self.request(path, body, headers={"Authorization": "Bearer " + token})[0], 401)
        status, data, _ = self.request(path, body, headers={"Authorization": "Bearer " + PEER})
        self.assertEqual((status, data["ok"], data["text"]), (200, True, "swarm cap up"))
        with self.assertRaisesRegex(self.m.WebError, "differ"):
            self.make_service(peer=TOKEN)

    def test_hub_routes_tools_over_peer_without_ssh(self):
        # Arrange two actual HTTP Services, with only terminal delivery replaced by a recorder.
        self.need_openssl()
        _, cookie = self.enroll()
        remote = self.m.RemoteAtc(self.url, TOKEN, background=False)
        row = {"sid": "session-beta", "name": "beta", "display_name": "beta", "kind": "interactive",
               "group": "waiting", "status": "idle", "cwd": "/workspace/example", "doing": "done"}
        self.service.model["machine"] = "laptop"
        self.service.model["groups"]["waiting"] = [row]
        remote.fetch()
        self.assertEqual(remote.every, 5)
        hub = self.make_service(origin=None)
        hub.remotes.append(remote)
        hub.model["hosts"] = [remote.view(time.time())]
        # Call through the browser-authenticated entry point on the hub.
        hub.web = self.service.web
        hub_url = self.start(hub)
        args = {"session": "session-beta", "host": "laptop", "text": "hello over the peer endpoint"}
        with mock.patch.object(self.m, "deliver", return_value=(True, "recorded")) as delivery:
            with mock.patch.object(self.m, "remote_tool", side_effect=AssertionError("must not use ssh")):
                # Act: preview must reach the peer but must not deliver anything.
                status, preview, _ = self.request("/api/web/tools/atc_message",
                                                   {"arguments": dict(args, dry_run=True)}, cookie, url=hub_url)
                self.assertEqual(status, 200)
                self.assertTrue(preview["ok"], preview)
                delivery.assert_not_called()
                status, result, _ = self.request("/api/web/tools/atc_message", {"arguments": args}, cookie, url=hub_url)
                # Assert: exactly one delivery, on the leaf Service's fleet, with the original message.
                self.assertEqual(status, 200)
                self.assertTrue(result["ok"], result)
                delivery.assert_called_once()
                self.assertIs(delivery.call_args[0][0], self.service.fleet)
                self.assertEqual(delivery.call_args[0][3], args["text"])
        with open(self.m.AUDIT_LOG) as fh:
            rows = [json.loads(line) for line in fh]
        calls = [r for r in rows if r.get("tool") == "atc_message" and r.get("outcome") == "done"]
        self.assertEqual({r["transport"] for r in calls}, {"web", "peer"})
        self.assertEqual({r["caller"] for r in calls}, {"Phone"})
        self.service.model["agents"] = [{"pid": 123456, "tool": "test-worker", "command": "example task"}]
        remote.fetch()
        hub.model["hosts"] = [remote.view(time.time())]
        with mock.patch.object(self.m, "stop_process", return_value="stopped test-worker") as stop:
            result = hub.run_tool("web", "atc_stop_process", {"arguments": {"host": "laptop", "pid": 123456}}, "Phone")
            self.assertTrue(result["ok"], result)
            stop.assert_called_once_with(None, 123456, False)
        with mock.patch.object(self.m.urllib.request, "urlopen", side_effect=OSError("offline")):
            remote.fetch()
        self.assertEqual(remote.every, 30)
        self.assertFalse(remote.view(time.time())["ok"])
        self.assertEqual(remote.data["groups"]["waiting"][0]["sid"], "session-beta")

    def test_tool_argument_validation_and_body_limits(self):
        path = "/api/peer/tools/atc_cap"
        headers = {"Authorization": "Bearer " + PEER}
        for args in [[], {"direction": "up", "dry_run": "false"}, {}, {"direction": 1}, {"direction": "up", "extra": True}]:
            self.assertEqual(self.request(path, {"arguments": args}, headers=headers)[0], 400)
        self.assertEqual(self.request(path, raw=b"[1]", headers=headers)[0], 400)
        self.assertEqual(self.request(path, raw=b"{", headers=headers)[0], 400)
        self.assertEqual(self.request(path, raw=b"x" * (self.m.WEB_BODY_LIMIT + 1), headers=headers)[0], 413)

    def test_origin_configuration_and_peer_file_permissions(self):
        for origin in ["http://atc.example.com", ORIGIN + "/", ORIGIN + "/path", ORIGIN + "?a=1", "https://owner@atc.example.com"]:
            with self.assertRaises(self.m.WebError):
                self.m.WebAuth(origin, os.path.join(self.tmp.name, "wrong"))
        directory = os.path.join(self.tmp.name, ".config", "atc")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "peer-token")
        self.assertIsNone(self.m.peer_token())
        self.assertFalse(os.path.exists(path))
        with open(path, "w") as fh:
            fh.write(PEER)
        os.chmod(path, 0o644)
        with self.assertRaises(self.m.WebError):
            self.m.peer_token()
        os.chmod(path, 0o600)
        self.assertEqual(self.m.peer_token(), PEER)

    def test_model_limits_and_json_safety(self):
        self.service.model.update(closed=[{"sid": str(i)} for i in range(60)],
                                  comms=[{"ts": i, "from": "one", "to": "two"} for i in range(210)])
        result = self.service.web_model()
        self.assertEqual(len(result["closed"]), 50)
        self.assertEqual(len(result["comms"]), 200)
        self.assertEqual(result["comms"][0]["ts"], 209)
        json.dumps(result)


class WebFleet(HTTP, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not fixture.TMUX:
            raise unittest.SkipTest("tmux is needed for the fixture's live sessions")
        cls.fx = fixture.Fixture().start()
        cls.env = mock.patch.dict(os.environ, cls.fx.env(ATC_PEER_TOKEN="", ATC_MCP_LEVERS="confirm"), clear=True)
        cls.env.start()
        cls.m = load_atc({})

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.fx.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="atc-web-fleet-")
        self.addCleanup(self.tmp.cleanup)
        self.service = self.make_service()
        self.url = self.start(self.service)
        # Identity protocol has independent real-vector tests above; these tests isolate the live levers.
        web = self.service.web
        web.data["keys"]["fixture-key"] = {"name": "Fixture passkey"}
        token = b64(secrets.token_bytes(32))
        web.sessions[token] = {"id": "fixture-key", "expires": time.time() + 60}
        self.cookie = "atc_session=" + token

    def make_service(self, remotes=(), only=None):
        m, fx = self.m, self.fx
        feed = m.Feed([os.path.join(fx.stub, "claude")], background=False)
        feed.fetch()
        repos = m.Repos(background=False)
        fleet = m.Fleet(only, feed, m.Herdr(None, background=False), repos=repos,
                        closed=m.ClosedSessions(repos=repos, background=False))
        quota = m.Quota([os.path.join(fx.stub, "quotamax")], background=False)
        quota.fetch()
        service = m.Service(fleet, quota, list(remotes), m.Terminals(), writes=True, token=TOKEN, background=False,
                            claude_cmd=[os.path.join(fx.stub, "claude")], web_origin=ORIGIN, peer_secret=PEER,
                            web_dir=os.path.join(self.tmp.name, "web-" + secrets.token_hex(4)))
        service.refresh()
        repos.fetch()
        service.refresh()
        return service

    def tool(self, name, args, url=None, cookie=None):
        status, result, _ = self.request("/api/web/tools/" + name, {"arguments": args}, cookie or self.cookie, url=url)
        self.assertEqual(status, 200, result)
        return result

    def test_session_detail_and_model(self):
        status, model, _ = self.request("/api/web/model", cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertEqual(model["counts"]["blocked"], 1)
        status, session, _ = self.request("/api/web/session/" + self.fx.sid("alpha-11"), cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertTrue(session["steps"])
        self.assertTrue(session["messages"])
        self.assertTrue(session["teams"])
        self.assertIn("repo", session)
        status, beta, _ = self.request("/api/web/session/" + self.fx.sid("beta-22"), cookie=self.cookie)
        self.assertIn("Want me to ship it?", beta["last_words"])
        self.assertEqual(self.request("/api/web/session/missing", cookie=self.cookie)[0], 400)

    def test_message_dry_run_real_pane_blocked_and_audit(self):
        text = "web fixture message " + secrets.token_hex(4)
        args = {"session": "beta-22", "host": "local", "text": text}
        dry = self.tool("atc_message", dict(args, dry_run=True))
        self.assertTrue(dry["ok"], dry)
        self.assertIn(text, dry["text"])
        self.assertIn("type", dry["text"])
        self.assertNotIn(text, self.fx.pane("beta-22"))
        result = self.tool("atc_message", args)
        self.assertTrue(result["ok"], result)
        time.sleep(0.3)
        self.assertIn(text, self.fx.pane("beta-22"))
        for dry_run in (True, False):
            refused = self.tool("atc_message", dict(args, session="gamma-33", dry_run=dry_run))
            self.assertFalse(refused["ok"])
            self.assertIn("blocked", refused["text"])
        self.assertNotIn(text, self.fx.pane("gamma-33"))
        with open(self.m.AUDIT_LOG) as fh:
            rows = [json.loads(line) for line in fh]
        row = next(r for r in reversed(rows) if r.get("arguments", {}).get("text") == text)
        self.assertEqual((row["transport"], row["caller"]), ("web", "Fixture passkey"))

    def test_self_guard_and_exit_unsaved(self):
        params = {"name": "atc_message", "arguments": {"session": "beta-22", "text": "self", "dry_run": True}}
        result = self.service.web_tools.call_tool(params, {"caps": {}}, self.fx.sid("beta-22"))
        self.assertTrue(result["isError"])
        self.assertIn("session calling", result["content"][0]["text"])
        result = self.tool("atc_close", {"session": "beta-22", "how": "exit", "dry_run": True})
        self.assertFalse(result["ok"])
        self.assertIn("uncommitted", result["text"])

    def test_peer_runs_real_message_and_hub_remote_transport(self):
        remote = self.m.RemoteAtc(self.url, TOKEN, background=False)
        remote.fetch()
        self.assertEqual(remote.every, 5)
        remote.host = "laptop"
        remote.data["machine"] = "laptop"
        hub = self.make_service([remote], only="/no-sessions-here")
        hub_url = self.start(hub)
        # Use the authenticated identity of this hub, never the machine bearer token.
        hub.web.data["keys"]["hub-key"] = {"name": "Hub passkey"}
        token = b64(secrets.token_bytes(32))
        hub.web.sessions[token] = {"id": "hub-key", "expires": time.time() + 60}
        cookie = "atc_session=" + token
        text = "hub peer message " + secrets.token_hex(4)
        args = {"session": self.fx.sid("beta-22"), "host": "laptop", "text": text}
        with mock.patch.object(self.m, "remote_tool", side_effect=AssertionError("HTTP host must not use ssh")):
            dry = self.tool("atc_message", dict(args, dry_run=True), hub_url, cookie)
            self.assertTrue(dry["ok"], dry)
            self.assertNotIn(text, self.fx.pane("beta-22"))
            done = self.tool("atc_message", args, hub_url, cookie)
            self.assertTrue(done["ok"], done)
        time.sleep(0.3)
        self.assertIn(text, self.fx.pane("beta-22"))
        refused = self.tool("atc_message", dict(args, session=self.fx.sid("gamma-33")), hub_url, cookie)
        self.assertFalse(refused["ok"])
        self.assertNotIn(text, self.fx.pane("gamma-33"))
        with open(self.m.AUDIT_LOG) as fh:
            rows = [json.loads(line) for line in fh]
        peer = [r for r in rows if r["transport"] == "peer" and r.get("arguments", {}).get("text") == text]
        self.assertTrue(any(r["outcome"] == "done" and r["caller"] == "Hub passkey" for r in peer))
        # Backoff keeps the last model while the host stops answering.
        with mock.patch.object(self.m.urllib.request, "urlopen", side_effect=OSError("offline")):
            remote.fetch()
        self.assertEqual(remote.every, 30)
        self.assertFalse(remote.view(time.time())["ok"])
        self.assertTrue(remote.data["groups"])
        hub.refresh()
        self.assertIn("isn't answering", self.tool("atc_message", dict(args, dry_run=True), hub_url, cookie)["text"])


if __name__ == "__main__":
    unittest.main()
