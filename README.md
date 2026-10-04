# atc

**Air traffic control for your coding agents.** One terminal screen for every Claude Code session, subagent,
workflow and agent process on your Mac and your servers, with the levers to steer them.

```text
 atc  18:39:04  14 sessions · 1 blocked · 2 waiting · 4 working · helpers 6/9 · buildbox 1 sessions 3 agents (1 stale)
quota  Claude 5h 10% · week 61%   Codex weekly 19%   Kimi 5h 1% · weekly 12%   DeepSeek $11.42   │ swarm cap ≤4 comfortable (auto)
── Blocked on you (1) ───────────────────────────────────────────────────────── a prompt or question is open ──
▶ ! payments-api       needs you 12s   permission prompt: Bash  Run the database migration
── Waiting on you (2) ──────────────────────────────────────────────────────────────────────── newest first ──
  ◆ docs-site          waiting 9m      “The changelog is drafted; two entries need a decision from you:”
  ◆ mobile-app         waiting 41m     “Build 12 is on TestFlight.”
── Working (4) ─────────────────────────────────────────────────────────────────────────── most recent first ──
  ● web-app            busy 1m         Bash  Run the end-to-end suite
       ⎿ release-train  ✓ Build  ▸ Verify   ████████░░ 4/5 done
           ✓ API client                 5m  done
           ● Smoke tester               3s  Bash  Hit the staging health check
  ● data-pipeline      busy 12m        Edit  src/loaders/events.py
       ⎿ ● Backfill October partitions   49s  Read src/loaders/backfill.py
       ⎿ ⚙ pid 4121 up 3m  codex exec "write tests for the parser"
── buildbox ──────────────────────────────────────────────────────────────────────────── 0.6s ssh · 2s ago ──
  ⚙ codex              up 5d         stale? under openclaw  pid 960599
```

## Why

Run a few Claude Code sessions at once, each fanning out subagents and workflows, add a Codex or Kimi job
and an agent on a server, and you lose track: which one is stuck on a permission prompt, which finished an
hour ago and is waiting for you, which background process has been running for five days. `atc` reads what
the agents already write to disk, puts it on one screen, and lets you act on it without hunting for the tab.

## What it shows

- **Blocked on you**: sessions sitting on a permission prompt or a question, and what they are asking
- **Waiting on you**: sessions that finished their turn, with the first line of what they said
- **Working**: what each busy session is doing now, its workflow phases and progress, its subagents, and any
  agent processes it started (`codex`, `kimi`, `claude -p` …)
- **Parked / background / other**: sessions open but idle for 12h+, `claude --bg` sessions, and sessions with
  only a shell running. Listed by default, so nothing open is invisible; `p` folds them away
- **Unsaved work**: each session's repo, checked in the background: uncommitted files, and commits that are on no
  remote yet, so a session isn't closed (or a laptop wiped) with work that only exists on disk
- **Other agents**: agent CLIs running outside any session, with what started them and a `stale?` flag after a day
- **Your servers**: the same view for every machine in `~/.config/atc/hosts`, over ssh
- **Herdr agents**: when [Herdr](https://herdr.dev) is running, the agents in its panes (Codex, opencode, pi …)
- **Hierarchy** (`h`): session › workflow › phase › agent › nested agent › worker process, who talks to whom,
  and each server's agents grouped by what started them
- **Comms** (`c`): every message, in full: session to session (✉), the task a session gave each helper (→),
  and the helper's report back (←)
- **Activity**: one merged log of every tool call across every session and helper
- **Quota** (with [quotamax](https://github.com/jhammant/quotamax)): Claude, Codex, Kimi, DeepSeek, and the
  swarm cap that sessions check before fanning out

## Levers

| Key | Does |
|---|---|
| `enter` | Jump to the session's terminal tab (iTerm2, Terminal.app, tmux, Herdr). A background session opens attached in a new tab |
| `enter` on a subagent | Drill in: who started it, its task, every step it took, its report, its own helpers |
| `enter` on a process or server row | Its command, uptime and what started it; on a server's session, an ssh shell there in a new tab |
| `m` | Type a message into the selected session, as if you typed it there. A busy session queues it |
| `M` | The same message to every session in the selected session's project folder |
| `x` `x` | Interrupt the session (Esc). A background session: `claude stop`. An agent process, here or on a server: stop it (SIGTERM, over ssh) |
| `C` `C` / `F` `F` | Type `/closecode` or `/forkcode` into the session: wrap it up, or split work off |
| `+` `-` `0` | Raise, lower or reset the swarm cap (`quotamax override`, pinned for 2h by default) |
| `n` | New task: `route --plan-only` says where it should run (Claude, Codex, Kimi …); `enter` dispatches it in a new tab |
| `c` `h` `tab` `p` `?` `q` | Comms, hierarchy, bottom panel, fold parked sessions away, help, quit |

**Steering never types into a session that is blocked on a prompt.** A permission prompt's default answer is
"Yes", so a message ending in Enter would approve whatever it was asking. `atc` re-reads the session's state
right before typing and skips it; press `enter` to go and answer the prompt yourself. Sessions in Herdr panes
are steered through `herdr agent prompt`, which has the same guard.

## Install

Python 3.9+, standard library only. macOS or Linux.

```bash
git clone https://github.com/jhammant/atc ~/dev/atc
ln -s ~/dev/atc/atc.py ~/.local/bin/atc
atc
```

```bash
atc                  # live view
atc --here           # only sessions in or under this folder: one project's swarm
atc --once           # one frame as text        atc --tree     the hierarchy
atc --comms          # every message            atc --json     the data, for scripts and menu-bar apps
atc --host buildbox  # add a machine for this run (or list them in ~/.config/atc/hosts)
atc --jump <id>      # focus a session's tab    atc --cap up|down|auto   change the swarm cap
atc --open           # open the live view in a new terminal tab
atc --dry-run        # levers say what they would do instead of doing it
```

### Servers

List ssh targets in `~/.config/atc/hosts`, one per line. `atc` runs itself on each over ssh
(`ssh host python3 - --json < atc.py`), so there is nothing to install there; the host needs Python 3.9+ and
key-based ssh. Two names for one machine are shown once. Server rows can be selected: open a process to see
what started it and stop it with `x x`, or press `enter` on a server's session for an ssh shell there.

### Where new sessions open

New sessions (`n`, attaching a background session, server shells) open in the terminal `atc` runs in. Set
`ATC_TERMINAL` to choose: `iterm`, `terminal`, `tmux` (a window in your current tmux), or `tmux-bg`: windows in
a detached `atc` tmux session you can attach to from anywhere, including over ssh from a phone.

### Optional tools

Used when found on `PATH`; override with `ATC_QUOTAMAX`, `ATC_ROUTE`, `ATC_HERDR`, `ATC_CLAUDE`, and `ATC_SSH`
(e.g. `ssh -J bastion`).

- [quotamax](https://github.com/jhammant/quotamax): quota for every provider, and the swarm cap
- `route`: picks where a new task should run
- [Herdr](https://herdr.dev): agents in Herdr panes, steered through Herdr. `atc` only talks to a Herdr
  server that is already running and never starts one, because starting it resumes every saved agent.

## Tests

```bash
python3 -m unittest discover -s tests -v    # needs tmux
```

The suite builds a fake Claude Code home (sessions blocked on a prompt, waiting, working with nested
subagents and a two-phase workflow, a background session, letters between sessions) and runs dummy
`claude` processes in a hidden tmux session. Unit tests cover the parsers and the model. Screen tests run
`atc` in tmux, press every key, read the screen back as drawn, and check steering in the dummy sessions' own
terminals: a message arrives, a blocked session receives nothing, Esc lands on interrupt. They also fail if
the screen loop ever stalls, or if any command `atc` runs is handed the terminal as its input.

If keys ever seem ignored, `ATC_DEBUG_LOG=/tmp/atc.log atc` logs every refresh and every pass of the screen
loop with how long it took and which keys arrived.

## How it works

Everything is read locally (or over your own ssh); nothing is sent anywhere else.

- `claude agents --json`: Claude Code's own list of active sessions, interactive and background, and whether
  one is waiting for input. Without it, `atc` falls back to `~/.claude/sessions/*.json`.
- `~/.claude/projects/<project>/<session>.jsonl`: the transcripts, read incrementally from near the end, plus
  each session's `subagents/` (with `parentAgentId` for nesting) and workflow journals.
- `ps`: each session's terminal, and agent processes.

The transcript format is Claude Code's internal format and can change between versions; `atc` skips records
it does not understand rather than failing.

## License

MIT
