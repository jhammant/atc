# AGENTS.md: working on atc

atc is air traffic control for coding agents: one view of every Claude Code session, subagent, workflow and
agent process on this machine and on configured servers, with levers to steer them. It is public (MIT).

## Shape

- `atc.py` is the program: one file, **Python 3.9+, standard library only**. No third-party packages, ever. It must
  parse and run under Python 3.9 (macOS launchd uses `/usr/bin/python3` 3.9.6): no `match`, no `X | Y` unions
  evaluated at runtime, no parenthesised context managers, no f-strings that reuse the outer quote inside `{}`.
- `taxi/` is the Taxi schema for `atc --serve`. `tests/` holds the suite; `tests/fixture.py` builds a fake Claude
  home and runs dummy `claude` processes in a hidden tmux session.
- Rough map of `atc.py`, top to bottom: readers (`Tail`, `Swarms`, `Agent`/`Helper`/`Session`, `Feed`, `Repos`, `AMail`, `Loom`,
  `ClosedSessions`, `Procs`, `Herdr`, `Remote`, `RemoteAtc`), `Fleet` and `Fleet.model()` (the one view model that
  the screen, `--json`, `--serve` and MCP all use), view helpers, levers (`deliver`, `interrupt_session`, `jump_to`,
  `stop_process`, `open_tab`, `Quota`), the HTTP service (`Service`, `Handler`), MCP (`@mcp_tool` registry `TOOLS`,
  `Cockpit`, `McpServer`, `mcp_stdio`), rendering, the curses `App`, `main()`.

## Commands

```bash
python3 -m unittest discover -s tests -v                 # everything; needs tmux and ps; about 2 minutes
python3 -m unittest discover -s tests -p test_mcp.py     # MCP and HTTP only; about 20 s
node --test tests/test_web_ui.js                        # browser polling, rendering, levers and approvals
node --test tests/test_*.js                            # every JavaScript test file
node --check web/app.js                               # browser syntax
ruff check --select F,E9 atc.py tests/                   # lint (errors only)
/usr/bin/python3 -c 'import ast; ast.parse(open("atc.py").read())'   # still Python 3.9
```

In a sandbox that denies `ps` or the tmux socket, fixture tests skip with a reason. Say which ones skipped; they
are run outside the sandbox before merging.

## Rules

- **Public repo.** No real hostnames, IP addresses, usernames, tokens or personal details in code, tests, docs or
  commit messages. Use placeholders (`laptop`, `server1`, `100.64.0.1`, `example.com`).
- **Read-only by default.** atc reads what agents already write to disk and what CLIs print. It never writes into
  Claude Code's files, never starts the Herdr server, and skips transcript records it does not understand.
- **Never type into a session blocked on a prompt.** Its default answer is "Yes", so Enter would approve it. Every
  path that types goes through `deliver()`, which re-checks the state right before typing.
- **Every lever** works from the screen, `--serve` and MCP through the same function, has a dry run, is refused for
  the calling session unless asked explicitly, needs the user's confirmation when it steers a session (MCP
  elicitation, a passkey in the web UI), and is written to the audit log.
- **Nothing slow on the screen loop.** Anything that runs a command or touches the network polls in a background
  thread (see `Feed`, `Repos`, `Remote`); the screen reads the last result.
- **Secrets** come from files with mode `0600` or the environment, are compared with `hmac.compare_digest`, and are
  never printed or logged.
- Match the surrounding code: plain names, few comments, short user-facing sentences in plain English.
- Update `README.md` for anything a user would notice. Tests use the fixture, in Arrange/Act/Assert order.

## Your final message

Files changed; decisions and assumptions; the exact test commands with pass/skip/fail counts; anything you could
not verify (network, real Claude Code, real terminals, other machines).
