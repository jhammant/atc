"""The Taxi schema against the public Taxi playground: it compiles, reads work, the session hop is discovered,
and the write compiles. Needs the internet, so it runs only when asked:

    ATC_TEST_PLAYGROUND=1 python3 -m unittest discover -s tests -p test_taxi.py -v
"""
import json
import os
import unittest
import urllib.error
import urllib.request

SCHEMA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "taxi", "src", "atc.taxi")
PLAYGROUND = "https://playground.taxilang.org/api/validate"

SESSIONS = [
    {"id": "s1", "name": "payments-api", "title": "Refunds", "kind": "interactive", "state": "blocked",
     "status": "waiting", "folder": "/w/payments", "repoRoot": "/w/payments", "doing": "permission prompt: Bash",
     "lastWords": None, "stateSeconds": 30, "uncommittedFiles": 0, "unpushedCommits": 2, "helpersWorking": 0,
     "host": "local"},
    {"id": "s2", "name": "web-app", "title": "Release", "kind": "interactive", "state": "working", "status": "busy",
     "folder": "/w/web", "repoRoot": "/w/web", "doing": "Bash  e2e", "lastWords": None, "stateSeconds": 60,
     "uncommittedFiles": 3, "unpushedCommits": 0, "helpersWorking": 2, "host": "local"},
]
SUBAGENTS = [
    {"sessionId": "s2", "label": "Smoke tester", "phase": "Verify", "modelName": None, "working": True,
     "finished": False, "doing": "Bash"},
    {"sessionId": "s2", "label": "Visual diff", "phase": "Verify", "modelName": "haiku", "working": True,
     "finished": False, "doing": "Read"},
]


def validate(query, stubs, expected):
    with open(SCHEMA) as fh:
        payload = {"schema": fh.read(), "query": query, "parameters": {}, "stubs": stubs,
                   "expectedJson": json.dumps(expected)}
    req = urllib.request.Request(PLAYGROUND, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        return {"valid": False, "errors": [error.read().decode()[:500]]}


@unittest.skipUnless(os.environ.get("ATC_TEST_PLAYGROUND"), "set ATC_TEST_PLAYGROUND=1 to check the schema online")
class Playground(unittest.TestCase):
    def assertValid(self, result):
        self.assertTrue(result.get("isValid", result.get("valid")), result.get("errors") or result)

    def test_read_sessions(self):
        self.assertValid(validate(
            "find { atc.AgentSession[] } as { name: atc.SessionName  state: atc.SessionState }[]",
            [{"operationName": "listSessions", "response": json.dumps(SESSIONS)}],
            [{"name": "payments-api", "state": "blocked"}, {"name": "web-app", "state": "working"}]))

    def test_subagent_reaches_its_session(self):
        self.assertValid(validate(
            "find { atc.Subagent[] } as { label: atc.SubagentLabel  session: atc.SessionName }[]",
            [{"operationName": "listSubagents", "response": json.dumps(SUBAGENTS)},
             {"operationName": "getSession", "response": json.dumps(SESSIONS[1])}],
            [{"label": "Smoke tester", "session": "web-app"}, {"label": "Visual diff", "session": "web-app"}]))

    def test_message_a_session(self):
        answer = {"sessionId": "s2", "delivered": True, "detail": "type: done"}
        self.assertValid(validate(
            'given { message: atc.SessionMessage = { sessionId: "s2", text: "status please" } } '
            "call atc.AtcApi::messageSession",
            [{"operationName": "messageSession", "response": json.dumps(answer)}], answer))


if __name__ == "__main__":
    unittest.main()
