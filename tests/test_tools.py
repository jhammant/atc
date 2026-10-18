"""Tools, profiles and the real exec boundary, using fake homes and generic server definitions."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import fixture
from test_units import load_atc


class ToolsFixture:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="atc-tools-")
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.stub = self.root / "stub"
        self.projects = self.home / ".claude" / "projects"
        self.work = self.home / "dev" / "project" / "repo"
        for path in (self.stub, self.projects, self.work):
            path.mkdir(parents=True)
        self.secret = "fixture-private-token-must-not-appear"
        self.definitions = {
            "notes": {"command": "node", "args": ["/opt/example/notes.js"], "env": {"TOKEN": self.secret}},
            "search": {"command": "npx", "args": ["-y", "@example/search-server@1.0.0"]},
            "remote": {"type": "http", "url": "https://example.com/mcp", "headers": {"Authorization": self.secret}},
        }
        self.config = self.home / ".claude.json"
        self.config.write_text(json.dumps({"mcpServers": self.definitions}))
        self.config.chmod(0o600)
        self.bin = self.stub / "claude"
        self.bin.write_text(f"#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
        self.bin.chmod(0o755)
        self.env = {**os.environ, "HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
                    "ATC_CLAUDE": str(self.bin), "ATC_PROFILES": "", "ATC_HOSTS": "", "ATC_QUOTAMAX": "",
                    "ATC_AMAIL": "", "ATC_LOOM": "", "ATC_LOOM_HOSTS": "", "ATC_HERDR": "",
                    "ATC_TERMINAL": "none", "PATH": str(self.stub) + os.pathsep + os.environ["PATH"]}
        self.env.pop("CLAUDE_CODE_SESSION_ID", None)

    def stop(self):
        self.tmp.cleanup()

    def transcript(self, records, name="session.jsonl"):
        path = self.projects / "project" / name
        fixture.write_jsonl(str(path), records)
        return path

    def record(self, ts, *blocks, cwd=None):
        return {"type": "assistant", "cwd": str(cwd or self.work), "timestamp": ts,
                "message": {"content": list(blocks)}}

    def profiles(self, strict=False, folder=None):
        directory = self.home / ".config" / "atc" / "profiles"
        directory.mkdir(parents=True, exist_ok=True)
        index = {"project": {"folders": [str(folder or self.work)], "servers": ["notes", "remote"], "strict": strict}}
        for name, data in (("profiles", index), ("project", {"mcpServers": {k: self.definitions[k] for k in ("notes", "remote")}})):
            path = directory / (name + ".json")
            path.write_text(json.dumps(data))
            path.chmod(0o600)
        return directory, index


class Base(unittest.TestCase):
    def setUp(self):
        self.fx = ToolsFixture()
        self.addCleanup(self.fx.stop)
        patch = mock.patch.dict(os.environ, self.fx.env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.m = load_atc(self.fx.env)

    def cli(self, *args, env=None, cwd=None):
        return subprocess.run([sys.executable, fixture.ATC, *args], env=env or dict(os.environ),
                              cwd=cwd or self.fx.work, capture_output=True, text=True, timeout=15)


class Profiles(Base):
    def test_longest_prefix_boundaries_tilde_and_symlinks(self):
        base = {"folders": ["/"], "servers": [], "strict": False}
        index = {"base": base, "area": dict(base, folders=["~/dev/project"]),
                 "repo": dict(base, folders=[str(self.fx.work)])}
        link = self.fx.root / "link"
        link.symlink_to(self.fx.work, target_is_directory=True)

        for folder, expected in ((self.fx.work / "src", "repo"), (link / "src", "repo"),
                                 (self.fx.home / "dev/project/other", "area"),
                                 (self.fx.home / "dev/project-other", "base")):
            with self.subTest(folder=folder):
                self.assertEqual(self.m.choose_profile(str(folder), index)[0], expected)
        self.assertIsNone(self.m.choose_profile("/outside", {"repo": index["repo"]}))

    def test_exec_strict_and_argument_order_with_spaces(self):
        directory, _index = self.fx.profiles(strict=True)
        args = ["--model", "example", "--resume", "id with spaces", "say 'hello'", "--", "literal"]

        result = self.cli("claude", *args)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["--mcp-config=" + str(directory / "project.json"),
                                                   "--strict-mcp-config"] + args)
        self.assertNotIn(self.fx.secret, result.stdout + result.stderr)

    def test_non_strict_only_adds_mcp_config(self):
        directory, _ = self.fx.profiles(strict=False)
        result = self.cli("claude", "--continue")
        self.assertEqual(json.loads(result.stdout), ["--mcp-config=" + str(directory / "project.json"), "--continue"])

    def test_prompt_stays_positional_with_commander_variadic_parsing(self):
        parser = Path(__file__).parent / "stubs" / "claude_variadic.py"
        self.fx.bin.write_text(f"#!{sys.executable}\n" + parser.read_text())
        # Prove the fixture catches the original separated-option regression.
        old = subprocess.run([str(self.fx.bin), "--mcp-config", "profile.json", "a prompt"],
                             capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(old.stdout)["configs"], ["profile.json", "a prompt"])
        for strict in (False, True):
            directory, _ = self.fx.profiles(strict=strict)
            for args, prompts, options in (
                    (["a prompt"], ["a prompt"], []),
                    (["a prompt", "--model", "example"], ["a prompt"], ["--model", "example"]),
                    (["--resume", "session id", "a prompt"], ["a prompt"], ["--resume", "session id"]),
                    (["--", "-literal prompt"], ["-literal prompt"], [])):
                with self.subTest(strict=strict, args=args):
                    result = self.cli("claude", *args)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    parsed = json.loads(result.stdout)
                    self.assertEqual(parsed["configs"], [str(directory / "project.json")])
                    self.assertEqual(parsed["positionals"], prompts)
                    self.assertEqual(parsed["options"], (["--strict-mcp-config"] if strict else []) + options)

    def test_every_passthrough_case(self):
        directory, _ = self.fx.profiles()
        for args in (["mcp", "list"], ["mcp", "add", "server"], ["update"], ["--version"], ["-v"],
                     ["-p", "prompt"], ["--print", "prompt"], ["--model", "example", "-p", "prompt"],
                     ["--print=prompt"], ["-pprompt"]):
            with self.subTest(args=args):
                result = self.cli("claude", *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), args)
        args = ["--resume", "keep", "a b"]
        off = self.cli("claude", *args, env={**os.environ, "ATC_PROFILES": "off"})
        self.assertEqual(json.loads(off.stdout), args)
        outside = self.cli("claude", *args, cwd=self.fx.root)
        self.assertEqual(json.loads(outside.stdout), args)
        (directory / "project.json").unlink()
        missing = self.cli("claude", *args)
        self.assertEqual(json.loads(missing.stdout), args)

    def test_unreadable_invalid_and_inconsistent_profiles_fall_through(self):
        directory, _ = self.fx.profiles()
        path = directory / "project.json"
        original = path.read_text()
        for content in ("{broken", "[]", '{"mcpServers":{}}'):
            path.write_text(content)
            self.assertEqual(json.loads(self.cli("claude", "continue").stdout), ["continue"])
        path.write_text(original)
        path.chmod(0o644)
        self.assertEqual(json.loads(self.cli("claude").stdout), [])
        path.chmod(0o600)
        (directory / "profiles.json").write_text("{broken " + self.fx.secret)
        self.assertEqual(json.loads(self.cli("claude", "resume").stdout), ["resume"])

    def test_readers_errors_and_dry_run_never_print_config_values(self):
        directory, _ = self.fx.profiles()
        args = ["--resume", "name with spaces", "quote'and$dollar"]
        before = self.fx.config.read_bytes()
        for command in (("profiles", "list"), ("profiles", "show", "project"),
                        ("profiles", "which"), ("profiles", "doctor"), ("claude", "--dry-run", *args)):
            result = self.cli(*command)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(self.fx.secret, result.stdout + result.stderr)
            self.assertNotIn("Authorization", result.stdout + result.stderr)
            self.assertNotIn("https://example.com/mcp", result.stdout + result.stderr)
        dry = self.cli("claude", "--dry-run", *args)
        self.assertEqual(shlex.split(dry.stdout), [str(self.fx.bin), "--mcp-config=" + str(directory / "project.json")] + args)
        self.assertEqual(before, self.fx.config.read_bytes())
        (directory / "profiles.json").write_text('{"bad": "' + self.fx.secret + '"}')
        for command in (("profiles", "show", "project"), ("profiles", "which"), ("profiles", "doctor")):
            result = self.cli(*command)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(self.fx.secret, result.stdout + result.stderr)
        self.fx.config.write_text("{broken " + self.fx.secret)
        result = self.cli("profiles", "init", "--force")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(self.fx.secret, result.stdout + result.stderr)

    def test_init_filters_existing_servers_and_refuses_overwrite(self):
        before = self.fx.config.read_bytes()
        index = self.m.init_profiles()
        path = Path(self.m.PROFILES_DIR)
        self.assertEqual(index, {"base": {"folders": ["/"], "servers": ["notes", "remote", "search"], "strict": False}})
        original = (path / "base.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "already exist"):
            self.m.init_profiles()
        self.assertEqual((path / "base.json").read_bytes(), original)
        self.m.init_profiles(force=True)
        for file in path.glob("*.json"):
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        definitions = json.loads((path / "base.json").read_text())["mcpServers"]
        self.assertEqual(definitions, self.fx.definitions)
        self.assertEqual(before, self.fx.config.read_bytes())
        self.assertNotEqual(self.cli("profiles", "init").returncode, 0)
        self.assertEqual(self.cli("profiles", "init", "--force").returncode, 0)
        self.assertFalse((self.fx.home / ".zshrc").exists())

    def test_import_starters_filters_servers_defaults_additive_and_refuses_overwrite(self):
        starter = self.fx.root / "starter.json"
        data = {"base": {"folders": ["/"], "servers": ["notes", "missing", "notes"]},
                "project": {"folders": [str(self.fx.work)], "servers": ["remote"], "strict": True}}
        starter.write_text(json.dumps(data))
        before = starter.read_bytes(), self.fx.config.read_bytes()
        result = self.cli("profiles", "init", "--from", str(starter))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(self.fx.secret, result.stdout + result.stderr)
        index = self.m.profile_index()
        self.assertEqual(index["base"], {"folders": ["/"], "servers": ["notes"], "strict": False})
        self.assertTrue(index["project"]["strict"])
        files = {p: p.read_bytes() for p in Path(self.m.PROFILES_DIR).glob("*.json")}
        self.assertNotEqual(self.cli("profiles", "init", "--from", str(starter)).returncode, 0)
        self.assertTrue(all(p.read_bytes() == content for p, content in files.items()))
        self.assertEqual(self.cli("profiles", "init", "--from", str(starter), "--force").returncode, 0)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in files))
        self.assertEqual(before, (starter.read_bytes(), self.fx.config.read_bytes()))

    def test_import_invalid_starters_never_writes_or_prints_values(self):
        starter = self.fx.root / "starter.json"
        for data in ([self.fx.secret], {"base": {"folders": ["/"], "servers": [self.fx.secret], "strict": "yes"}},
                     {"profiles": {"folders": ["/"], "servers": []}},
                     {"../escape": {"folders": ["/"], "servers": []}}):
            starter.write_text(json.dumps(data))
            result = self.cli("profiles", "init", "--from", str(starter))
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(self.fx.secret, result.stdout + result.stderr)
            self.assertFalse(Path(self.m.PROFILES_DIR).exists())

    def test_index_strict_defaults_false_and_doctor_warns_each_strict_profile(self):
        directory, index = self.fx.profiles()
        del index["project"]["strict"]
        (directory / "profiles.json").write_text(json.dumps(index))
        self.assertFalse(self.m.profile_index()["project"]["strict"])
        self.assertNotIn("--strict-mcp-config", json.loads(self.cli("claude", "prompt").stdout))
        self.m.init_profiles(force=True)
        index.update(self.m.profile_index())
        for row in index.values():
            row["strict"] = True
        (directory / "profiles.json").write_text(json.dumps(index))
        result = self.cli("profiles", "doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in index:
            self.assertIn(name + ": warning: strict drops claude.ai connectors, plugin and project servers", result.stdout)

    def test_doctor_reports_removed_and_unassigned_servers(self):
        self.fx.profiles()
        del self.fx.definitions["remote"]
        self.fx.config.write_text(json.dumps({"mcpServers": self.fx.definitions}))
        result = self.cli("profiles", "doctor")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no longer configured: remote", result.stdout)
        self.assertIn("Servers in no profile: search", result.stdout)
        self.assertNotIn(self.fx.secret, result.stdout + result.stderr)

    def test_path_lookup_skips_atc_symlink_and_honors_override(self):
        shim = self.fx.root / "shim"
        shim.mkdir()
        (shim / "claude").symlink_to(fixture.ATC)
        with mock.patch.dict(os.environ, {"ATC_CLAUDE": "", "PATH": str(shim) + os.pathsep + str(self.fx.stub)}):
            self.assertEqual(self.m.real_claude(), [str(self.fx.bin)])
        with mock.patch.dict(os.environ, {"ATC_CLAUDE": shlex.quote(str(self.fx.bin)) + " --model example"}):
            self.assertEqual(self.m.real_claude(), [str(self.fx.bin), "--model", "example"])
        with mock.patch.dict(os.environ, {"ATC_CLAUDE": fixture.ATC}):
            with self.assertRaises(ValueError):
                self.m.real_claude()

    def test_fifo_profile_falls_through_without_blocking(self):
        directory, _ = self.fx.profiles()
        path = directory / "project.json"
        path.unlink()
        os.mkfifo(str(path), 0o600)
        self.assertEqual(json.loads(self.cli("claude", "--continue").stdout), ["--continue"])

    def test_profile_symlinks_and_unsafe_index_rejected(self):
        directory, index = self.fx.profiles()
        profile = directory / "project.json"
        profile.unlink()
        profile.symlink_to(self.fx.config)
        self.assertIsNone(self.m.read_profile("project", index["project"]))
        self.assertEqual(json.loads(self.cli("claude").stdout), [])
        index["../escape"] = index.pop("project")
        (directory / "profiles.json").write_text(json.dumps(index))
        self.assertNotEqual(self.cli("profiles", "which").returncode, 0)


class Usage(Base):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.usage = self.m.ToolUsage(background=False)

    def server(self, data, name="notes"):
        return next(row for row in data["servers"] if row["name"] == name)

    def test_windows_skills_areas_duplicates_and_nested_helpers(self):
        day = 86400
        records = [self.fx.record(self.now - ago * day, fixture.tool(str(ago), "mcp__notes__read"))
                   for ago in (1, 8, 29, 31)]
        records += [records[0], self.fx.record(self.now - 2, fixture.tool("skill", "Skill", skill="review")),
                    {"type": "user", "timestamp": self.now, "message": {"content": [fixture.tool("bad", "mcp__notes__read")]}},
                    self.fx.record(self.now, fixture.tool("ignored", "Read", file_path="SKILL.md"))]
        self.fx.transcript(records)
        helper = self.fx.record(self.now - 5, fixture.tool("helper", "mcp__notes__read"))
        helper.pop("cwd")
        self.fx.transcript([helper], "session/subagents/helper.jsonl")
        self.fx.transcript([self.fx.record(self.now - 7, fixture.tool("other", "mcp__notes__read"),
                                          cwd=self.fx.home / "dev/other/repo")], "other.jsonl")

        data = self.usage.fetch(self.now)

        server = self.server(data)
        self.assertEqual((server["calls_7d"], server["calls_30d"], server["uses"]), (3, 5, 6))
        self.assertEqual(server["last_used"], self.now - 5)
        self.assertEqual(server["areas"], [str(self.fx.home / "dev/other"), str(self.fx.home / "dev/project")])
        skill = next(row for row in data["skills"] if row["name"] == "review")
        self.assertEqual((skill["uses"], skill["last_used"]), (1, self.now - 2))
        self.assertNotIn(self.fx.secret, json.dumps(data))

    def test_incremental_cache_partial_lines_truncate_replace_and_age_out(self):
        path = self.fx.transcript([self.fx.record(self.now - 10, fixture.tool("one", "mcp__notes__read"))])
        self.usage.fetch(self.now)
        old = self.usage.files[str(path)]
        with mock.patch("builtins.open", wraps=open) as opened:
            self.usage.fetch(self.now)
        self.assertIs(self.usage.files[str(path)], old)
        self.assertFalse(any(str(c.args[0]) == str(path) for c in opened.call_args_list))
        record = self.fx.record(self.now - 1, fixture.tool("two", "mcp__notes__read"))
        text = json.dumps(record)
        with path.open("a") as fh:
            fh.write(text[:20])
        self.assertEqual(self.server(self.usage.fetch(self.now))["calls_7d"], 1)
        with path.open("a") as fh:
            fh.write(text[20:] + "\n")
        self.assertEqual(self.server(self.usage.fetch(self.now))["calls_7d"], 2)
        path.write_text(json.dumps(record) + "\n")
        self.assertEqual(self.server(self.usage.fetch(self.now))["uses"], 1)
        replacement = path.with_suffix(".tmp")
        replacement.write_text(json.dumps(record).replace("two", "new") + "\n")
        replacement.replace(path)
        self.assertEqual(self.server(self.usage.fetch(self.now))["uses"], 1)
        aged = self.server(self.usage.fetch(self.now + 31 * 86400))
        self.assertEqual((aged["calls_7d"], aged["calls_30d"], aged["uses"]), (0, 0, 1))
        self.assertEqual(aged["last_used"], self.now - 1)
        path.unlink()
        self.assertEqual(self.server(self.usage.fetch(self.now))["uses"], 0)

    def test_bad_records_are_skipped_and_missing_config_is_sanitized(self):
        path = self.fx.transcript([None, [], {"type": "assistant", "message": []},
                                   self.fx.record(float("inf"), fixture.tool("infinite", "mcp__notes__read")),
                                   self.fx.record(self.now - 1, None, {"type": "tool_use", "name": "Skill", "input": []}),
                                   self.fx.record(self.now - 1, fixture.tool("x", "mcp__notes__read"))])
        with path.open("a") as fh:
            fh.write("{bad line\n")
        self.fx.config.write_text("{broken " + self.fx.secret)
        data = self.usage.fetch(self.now)
        self.assertEqual(self.server(data)["uses"], 1)
        self.assertIn("cannot read", data["error"])
        self.assertNotIn(self.fx.secret, json.dumps(data))

    def test_installed_global_project_and_plugin_skills_include_zero_use(self):
        self.fx.transcript([self.fx.record(self.now - 1)])
        global_skill = self.fx.home / ".claude/skills/review/SKILL.md"
        local_skill = self.fx.work / ".claude/skills/local/SKILL.md"
        plugin = self.fx.root / "plugin"
        for path in (global_skill, local_skill, plugin / "skills/check/SKILL.md"):
            path.parent.mkdir(parents=True)
            path.write_text("# A skill\n")
        registry = self.fx.home / ".claude/plugins/installed_plugins.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(json.dumps({"plugins": {"example@market": [{"installPath": str(plugin)}]}}))
        data = self.usage.fetch(self.now)
        self.assertEqual([r["name"] for r in data["skills"]], ["example:check", "local", "review"])
        self.assertTrue(all(r["uses"] == 0 for r in data["skills"]))
        self.assertEqual(self.m.find_skill({"tools": data}, "local", str(self.fx.work))[1]["path"], str(local_skill))
        with self.assertRaises(self.m.ToolError):
            self.m.find_skill({"tools": data}, "local", "/elsewhere")

    def test_plugin_scopes_and_project_paths_limit_skill_resolution(self):
        plugin = self.fx.root / "plugin"
        skill = plugin / "skills/check/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# Check\n")
        registry = self.fx.home / ".claude/plugins/installed_plugins.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(json.dumps({"plugins": {
            "global@market": [{"installPath": str(plugin), "scope": "user"}],
            "scoped@market": [{"installPath": str(plugin), "scope": scope, "projectPath": str(folder)}
                              for scope, folder in (("project", self.fx.work), ("local", self.fx.root / "other"))],
            "invalid@market": [{"installPath": str(plugin), "scope": "project"}],
        }}))
        data = self.usage.fetch(self.now)
        model = {"tools": data}
        self.assertEqual([r["name"] for r in data["skills"]], ["global:check", "scoped:check"])
        self.assertEqual(self.m.find_skill(model, "global:check", "/elsewhere")[1]["scope"], "user")
        for scope, folder in (("project", self.fx.work), ("local", self.fx.root / "other")):
            location = self.m.find_skill(model, "scoped:check", str(folder / "src"))[1]
            self.assertEqual(location["scope"], scope)
            self.assertEqual(location["projectPath"], str(folder))
            self.assertEqual(location["folder"], str(folder))
        link = self.fx.root / "linked"
        link.symlink_to(self.fx.work, target_is_directory=True)
        self.assertEqual(self.m.find_skill(model, "scoped:check", str(link))[1]["scope"], "project")
        for folder in ("", "/elsewhere", str(self.fx.work) + "-other"):
            with self.assertRaises(self.m.ToolError):
                self.m.find_skill(model, "scoped:check", folder)

    def test_process_cost_counts_launcher_tree_once_and_rejects_ambiguous(self):
        def proc(parent, command, rss):
            return {"ppid": parent, "etime": "01:00", "command": command, "rss": rss}
        table = {10: proc(1, "claude", 1000), 11: proc(10, "node /opt/example/notes.js", 2048),
                 12: proc(11, "node /opt/example/notes.js", 1024), 13: proc(10, "npx -y @example/search-server@1.0.0", 1024),
                 14: proc(13, "node /cache/node_modules/@example/search-server/index.js", 4096),
                 20: proc(1, "claude", 2000), 21: proc(20, "node /opt/example/notes.js", 1024),
                 30: proc(1, "node /opt/example/notes.js", 9999), 31: proc(10, "node /unrelated.js", 9999)}
        sessions = [SimpleNamespace(pid=10, sid="one"), SimpleNamespace(pid=20, sid="two")]
        self.usage.fetch(self.now)
        data = self.usage.model(table, sessions, self.now)
        notes = self.server(data)
        self.assertEqual((notes["copies"], notes["rss_kb"]), (2, 4096))
        self.assertEqual(self.server(data, "search")["rss_kb"], 5120)
        self.assertEqual(self.server(data, "remote")["copies"], 0)
        self.assertTrue(self.m.matches_server(self.fx.definitions["search"], table[14]["command"]))
        self.assertNotIn("command", json.dumps(data))
        ambiguous = dict(self.fx.definitions, duplicate=self.fx.definitions["notes"])
        self.assertNotIn("notes", self.m.server_processes(ambiguous, table, sessions, self.now))

    def test_json_cli_waits_for_scan_and_includes_safe_tools(self):
        self.fx.transcript([self.fx.record(self.now - 1, fixture.tool("one", "mcp__notes__read"))])
        ps = self.fx.stub / "ps"
        ps.write_text(f"#!{sys.executable}\n")
        ps.chmod(0o755)
        result = self.cli("--json", "--no-hosts")
        self.assertEqual(result.returncode, 0, result.stderr)
        model = json.loads(result.stdout)
        self.assertIsNotNone(model["tools"]["scanned_at"])
        self.assertEqual(self.server(model["tools"])["calls_7d"], 1)
        self.assertNotIn(self.fx.secret, result.stdout + result.stderr)
        self.assertNotIn("Authorization", result.stdout)

    def test_ps_parses_rss_without_truncating_commands(self):
        with mock.patch.object(self.m, "run", return_value=(0, "11 10 ttys001 01:00 2048 node /opt/example/notes.js --flag x\n", "")) as run:
            table = self.m.process_table()
        self.assertEqual(table[11]["rss"], 2048)
        self.assertEqual(table[11]["command"], "node /opt/example/notes.js --flag x")
        self.assertIn("rss=", run.call_args.args[0][-1])


class LeversAndViews(Base):
    def setUp(self):
        super().setUp()
        m, fx = self.m, self.fx
        self.now = time.time()
        path = fx.home / ".claude/skills/review/SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("# Review\n")
        usage = m.ToolUsage(background=False)
        usage.fetch(self.now)
        self.fleet = m.Fleet(tools=usage)
        self.fleet.procs = {
            10: {"ppid": 1, "etime": "02:00", "command": "claude", "rss": 10240, "tty": ""},
            11: {"ppid": 10, "etime": "01:00", "command": "node /opt/example/notes.js", "rss": 2048, "tty": ""}}
        self.fleet.children = {10: [11]}
        self.fleet.procs_at = self.now
        session = m.Session("idle-id", str(fx.work))
        session.pid = 10
        session.entry = {"status": "idle", "name": "idle-session"}
        session.registry = {"statusUpdatedAt": self.now - 1}
        session.kind, session.tty = "interactive", "/dev/fake"
        self.fleet.sessions = {session.sid: session}
        self.quota = m.Quota(None, background=False)
        self.model = self.fleet.model(self.now, self.quota)
        self.view = m.ordered_sessions(self.model, True)[0]
        self.terminals = mock.Mock()
        self.terminals.act.return_value = "type: done"
        self.fleet.feed = mock.Mock(cmd=["fake-claude"])
        self.fleet.feed.status_now.return_value = "idle"
        self.ck = m.Cockpit(self.fleet, self.quota, [], self.terminals, [str(fx.bin)], None,
                            service=SimpleNamespace(model=self.model, prefer=None))
        self.app = m.App(self.fleet, self.quota, self.terminals, None, [str(fx.bin)], True)

    def add_server_copy(self):
        self.fleet.procs[12] = dict(self.fleet.procs[11])
        # Mutate the published list as well as the ps fixture: reviewed snapshots must be independent.
        data = self.fleet.tools.model(self.fleet.procs, list(self.fleet.sessions.values()), self.now)
        row = next(r for r in self.model["tools"]["servers"] if r["name"] == "notes")
        fresh = next(r for r in data["servers"] if r["name"] == "notes")
        row["processes"][:] = fresh["processes"]
        row["copies"] = fresh["copies"]

    def confirming_server(self, **kwargs):
        with mock.patch.dict(os.environ, {"ATC_MCP_LEVERS": "confirm"}):
            return self.m.McpServer(self.ck, "http", **kwargs)

    def restart_call(self, server, ctx=None, **extra):
        ctx = ctx or {"modern": True, "caps": {"elicitation": {"form": {}}}, "client": None}
        return server.call_tool(dict(name="atc_restart_server", arguments={"server": "notes"}, **extra), ctx, "other-id")

    def test_tools_tab_rendering_at_narrow_and_wide_sizes_is_cached(self):
        m, app = self.m, self.app
        self.assertIn(("7", "tools", "Tools"), m.visible_tabs(self.model, app.ui))
        app.handle("7", self.model)
        with mock.patch.object(m.ToolUsage, "fetch", side_effect=AssertionError("scan on screen")), mock.patch.object(
                m, "run", side_effect=AssertionError("command on screen")), mock.patch("builtins.open", side_effect=AssertionError("IO on screen")):
            for width in (38, 90, 160):
                app.ui["footer"] = app.footer(self.now, self.model)
                frame, keys = m.render(self.fleet, self.model, app.ui, width, 30)
                text = "\n".join(m.plain(line) for line in frame)
                self.assertIn("7 Tools", text)
                self.assertIn("notes", text)
                self.assertIn("review", text)
                self.assertIn("skill::review", keys)
                self.assertIn("server::notes", keys)
                self.assertTrue(all(len(m.plain(line)) <= width for line in frame))
            self.assertNotIn(self.fx.secret, m.tool_tools(self.ck, {}, m.Call()))

    def test_skill_open_dry_run_and_run_confirmation_use_common_levers(self):
        m, app = self.m, self.app
        app.handle("7", self.model)
        app.ui["selected"] = "skill::review"
        with mock.patch.object(m, "open_tab", return_value="dry run: opened skill") as opened:
            app.handle("\n", self.model)
        self.assertIn("SKILL.md", opened.call_args.args[1])
        self.assertTrue(opened.call_args.args[2])
        app.handle("r", self.model)
        self.assertEqual(app.mode, "skill_pick")
        app.handle("\n", self.model)
        self.assertEqual(app.mode, "skill_confirm")
        self.assertIn("/review", m.plain(app.footer(self.now, self.model)))
        self.terminals.act.assert_not_called()
        app.dry_run = False
        app.handle("\n", self.model)
        self.terminals.act.assert_called_once_with("/dev/fake", "type", "/review")
        audit = Path(m.AUDIT_LOG).read_text()
        self.assertIn("atc_run_skill", audit)
        self.assertNotIn(self.fx.secret, audit)

    def test_run_skill_rechecks_blocked_busy_and_self(self):
        m = self.m
        args = {"skill": "review", "session": "idle-id"}
        for status in ("busy", "waiting", "blocked", None):
            with self.subTest(status=status):
                lever = m.tool_run_skill(self.ck, args, m.Call())
                self.fleet.feed.status_now.return_value = status
                ok, text = lever.run()
                self.assertFalse(ok, text)
                self.terminals.act.assert_not_called()
        with self.assertRaisesRegex(m.ToolError, "session calling"):
            m.tool_run_skill(self.ck, args, m.Call(caller="idle-id"))
        self.fleet.feed.status_now.return_value = "idle"
        self.assertTrue(m.tool_run_skill(self.ck, args, m.Call(caller="idle-id", allow_self=True)).run()[0])
        self.view["status"] = "busy"
        with self.assertRaisesRegex(m.ToolError, "idle session"):
            m.tool_run_skill(self.ck, args, m.Call())

    def test_herdr_skill_run_rechecks_herdr_instead_of_claude_feed(self):
        m = self.m
        self.fleet.herdr = m.Herdr(["fake-herdr"], background=False)
        view = m.herdr_view({"pane_id": "pane-1", "agent_status": "idle", "cwd": str(self.fx.work)}, self.now)
        self.model["groups"]["waiting"].append(view)
        args = {"skill": "review", "session": view["sid"]}
        for status in ("blocked", "working", None, "idle"):
            with self.subTest(status=status):
                lever = m.tool_run_skill(self.ck, args, m.Call())
                agents = [{"pane_id": "pane-1", "agent_status": status}] if status else []
                responses = [(0, "status: running", ""), (0, json.dumps({"result": {"agents": agents}}), "")]
                with mock.patch.object(m, "run", side_effect=responses) as run, mock.patch.object(
                        self.fleet.herdr, "act", return_value="type: done (herdr)") as act:
                    ok, _ = lever.run()
                    self.assertEqual(ok, status == "idle")
                    self.assertEqual(run.call_args_list, [mock.call(["fake-herdr", "status", "server"], timeout=10),
                                                         mock.call(["fake-herdr", "agent", "list"], timeout=10)])
                    if status == "idle":
                        act.assert_called_once_with("pane-1", "type", "/review")
                    else:
                        act.assert_not_called()
        self.fleet.feed.status_now.assert_not_called()
        self.terminals.act.assert_not_called()

    def test_herdr_delivery_refuses_unavailable_or_failed_state_refresh(self):
        m = self.m
        self.fleet.herdr = m.Herdr(["fake-herdr"], background=False)
        view = m.herdr_view({"pane_id": "pane-1", "agent_status": "idle"}, self.now)
        for responses in ([(1, "status: stopped", "")],
                          [(0, "status: running", ""), (1, "", "failed")]):
            self.fleet.herdr.agents = [{"pane_id": "pane-1", "agent_status": "idle"}]
            with mock.patch.object(m, "run", side_effect=responses), mock.patch.object(self.fleet.herdr, "act") as act:
                ok, text = m.deliver(self.fleet, self.terminals, view, "message")
                self.assertFalse(ok)
                self.assertIn("recheck Herdr", text)
                act.assert_not_called()
        self.fleet.feed.status_now.assert_not_called()

    def test_skill_picker_cancel_and_state_change_on_confirmation(self):
        app = self.app
        app.handle("7", self.model)
        app.ui["selected"] = "skill::review"
        app.handle("r", self.model)
        app.handle("\x1b", self.model)
        self.assertEqual(app.mode, "normal")
        app.handle("r", self.model)
        app.handle("\n", self.model)
        self.view["status"] = "busy"
        app.handle("\n", self.model)
        self.assertEqual(app.mode, "normal")
        self.assertIn("idle", app.flash[0])
        self.terminals.act.assert_not_called()

    def test_restart_dry_run_two_press_confirm_audit_and_self_guard(self):
        m, app = self.m, self.app
        app.handle("7", self.model)
        app.ui["selected"] = "server::notes"
        with mock.patch.object(m, "process_table", return_value=self.fleet.procs), mock.patch.object(m.os, "kill") as kill:
            app.handle("x", self.model)
            self.assertIn("press x again", app.flash[0])
            kill.assert_not_called()
            app.handle("x", self.model)
            self.assertIn("dry run", app.flash[0])
            self.assertIn("/mcp", app.flash[0])
            kill.assert_not_called()
            self.assertIn("atc_restart_server", Path(m.AUDIT_LOG).read_text())
            with self.assertRaisesRegex(m.ToolError, "session calling"):
                m.tool_restart_server(self.ck, {"server": "notes"}, m.Call(caller="idle-id"))
            lever = m.tool_restart_server(self.ck, {"server": "notes"}, m.Call(caller="idle-id", allow_self=True))
            self.assertTrue(lever.run()[0])
            kill.assert_called_once_with(11, m.signal.SIGTERM)

    def test_restart_rejects_replaced_process_and_config_without_killing(self):
        m = self.m
        lever = m.tool_restart_server(self.ck, {"server": "notes"}, m.Call())
        for change in ({"command": "node /unrelated.js"}, {"ppid": 999}, {"etime": "00:00"}):
            table = dict(self.fleet.procs, **{})
            table[11] = dict(table[11], **change)
            with mock.patch.object(m, "process_table", return_value=table), mock.patch.object(m.os, "kill") as kill:
                self.assertFalse(lever.run()[0])
                kill.assert_not_called()
        self.fx.config.write_text("{bad " + self.fx.secret)
        with mock.patch.object(m.os, "kill") as kill:
            ok, text = lever.run()
            self.assertFalse(ok)
            self.assertNotIn(self.fx.secret, text)
            kill.assert_not_called()

    def test_restart_mcp_confirmation_only_stops_the_reviewed_snapshot(self):
        m = self.m
        server = self.confirming_server()
        with mock.patch.object(m, "process_table", return_value=self.fleet.procs), mock.patch.object(m.os, "kill") as kill:
            question = self.restart_call(server)
            self.assertEqual(question["resultType"], "input_required")
            self.add_server_copy()
            answer = self.restart_call(server, requestState=question["requestState"],
                                       inputResponses={"confirm": {"action": "accept"}})
            self.assertFalse(answer["isError"], answer)
            kill.assert_called_once_with(11, m.signal.SIGTERM)
            replay = self.restart_call(server, requestState=question["requestState"],
                                       inputResponses={"confirm": {"action": "accept"}})
            self.assertEqual(replay["resultType"], "input_required")
            self.assertIn("2 copies", replay["inputRequests"]["confirm"]["params"]["message"])
            kill.assert_called_once()
        state = server.read_state(replay["requestState"], "atc_restart_server", {"server": "notes"}, "other-id")
        self.assertNotIn("/opt/example/notes.js", json.dumps(state))
        self.assertNotIn(self.fx.secret, json.dumps(state))

    def test_restart_same_pid_new_start_or_command_requires_new_confirmation(self):
        m = self.m
        for change in ({"etime": "00:01"}, {"command": "node /opt/example/notes.js --changed"}):
            with self.subTest(change=change):
                original = dict(self.fleet.procs[11])
                server = self.confirming_server()
                question = self.restart_call(server)
                self.fleet.procs[11].update(change)
                self.model["tools"] = self.fleet.tools.model(self.fleet.procs, list(self.fleet.sessions.values()), self.now)
                with mock.patch.object(m, "process_table", return_value=self.fleet.procs), mock.patch.object(m.os, "kill") as kill:
                    answer = self.restart_call(server, requestState=question["requestState"],
                                               inputResponses={"confirm": {"action": "accept"}})
                    self.assertTrue(answer["isError"])
                    self.assertIn("changed", answer["content"][0]["text"])
                    kill.assert_not_called()
                self.fleet.procs[11] = original
                self.model["tools"] = self.fleet.tools.model(self.fleet.procs, list(self.fleet.sessions.values()), self.now)

    def test_restart_pending_approval_without_snapshot_requires_new_question(self):
        m = self.m
        self.ck.approvals = m.Approvals()
        args = {"server": "notes"}
        lever = m.tool_restart_server(self.ck, args, m.Call())
        row = self.ck.approvals.create("atc_restart_server", args, lever.summary, "other-id", "fixture")
        self.ck.approvals.decide(row["id"], "approve", "owner", "terminal")
        with mock.patch.object(m.os, "kill") as kill:
            result = self.confirming_server().call_tool(
                {"name": "atc_restart_server", "arguments": dict(args, approval=row["id"])},
                {"modern": True, "caps": {}, "client": None}, "other-id")
            self.assertTrue(result["isError"])
            self.assertIn("no process snapshot", result["content"][0]["text"])
            kill.assert_not_called()

    def test_restart_pending_approval_binds_snapshot_for_wait_and_retry(self):
        m = self.m
        self.ck.approvals = m.Approvals()
        ctx = {"modern": True, "caps": {}, "client": None}
        for wait in (False, True):
            with self.subTest(wait=wait):
                self.fleet.procs.pop(12, None)
                self.model["tools"] = self.fleet.tools.model(self.fleet.procs, list(self.fleet.sessions.values()), self.now)
                server = self.confirming_server()

                def approve(row, args):
                    self.add_server_copy()
                    self.ck.approvals.decide(row["id"], "approve", "owner", "terminal")

                with mock.patch.object(m, "approval_wait", return_value=0), mock.patch.object(
                        m, "notify_approval", side_effect=approve if wait else None), mock.patch.object(
                        m, "process_table", return_value=self.fleet.procs), mock.patch.object(m.os, "kill") as kill:
                    result = self.restart_call(server, ctx)
                    row = self.ck.approvals.access()[-1]
                    self.assertEqual([p["pid"] for p in row["snapshot"]], [11])
                    if not wait:
                        kill.assert_not_called()
                        approve(row, {})
                        result = server.call_tool({"name": "atc_restart_server",
                                                   "arguments": {"server": "notes", "approval": row["id"]}}, ctx, "other-id")
                    self.assertFalse(result["isError"], result)
                    kill.assert_called_once_with(11, m.signal.SIGTERM)

    def test_restart_legacy_question_retains_snapshot(self):
        m = self.m

        def ask(question):
            self.assertIn("1 copies", question["message"])
            self.add_server_copy()
            return {"action": "accept"}

        server = self.confirming_server(ask=ask)
        ctx = {"modern": False, "caps": {"elicitation": {}}, "version": "2025-11-25", "client": None}
        with mock.patch.object(m, "process_table", return_value=self.fleet.procs), mock.patch.object(m.os, "kill") as kill:
            result = self.restart_call(server, ctx)
            self.assertFalse(result["isError"], result)
            kill.assert_called_once_with(11, m.signal.SIGTERM)

    def test_restart_screen_retains_reviewed_snapshot_between_presses(self):
        m, app = self.m, self.app
        app.dry_run = False
        app.handle("7", self.model)
        app.ui["selected"] = "server::notes"
        with mock.patch.object(m, "process_table", return_value=self.fleet.procs), mock.patch.object(m.os, "kill") as kill:
            app.handle("x", self.model)
            self.add_server_copy()
            app.handle("x", self.model)
            kill.assert_called_once_with(11, m.signal.SIGTERM)

    def test_web_restart_preview_is_bound_single_use_and_required(self):
        m = self.m
        self.terminals.dry_run = False
        service = m.Service(self.fleet, self.quota, [], self.terminals, background=False, writes=True,
                            claude_cmd=[str(self.fx.bin)], route_cmd=None)
        service.model, service.at = self.model, self.now
        for transport in ("web", "peer"):
            with self.subTest(transport=transport):
                self.fleet.procs.pop(12, None)
                self.model["tools"] = self.fleet.tools.model(self.fleet.procs, list(self.fleet.sessions.values()), self.now)
                with mock.patch.object(m, "process_table", return_value=self.fleet.procs), mock.patch.object(m.os, "kill") as kill:
                    body = {"arguments": {"server": "notes"}}
                    self.assertFalse(service.run_tool(transport, "atc_restart_server", body, "other-id")["ok"])
                    preview = service.run_tool(transport, "atc_restart_server",
                                               {"arguments": {"server": "notes", "dry_run": True}}, "other-id")
                    self.assertTrue(preview["ok"], preview)
                    self.add_server_copy()
                    body["requestState"] = preview["requestState"]
                    result = service.run_tool(transport, "atc_restart_server", body, "other-id")
                    self.assertTrue(result["ok"], result)
                    kill.assert_called_once_with(11, m.signal.SIGTERM)
                    self.assertFalse(service.run_tool(transport, "atc_restart_server", body, "other-id")["ok"])
                    kill.assert_called_once()

    def test_mcp_confirms_skill_run_and_supports_tools_with_levers_disabled(self):
        m = self.m
        with mock.patch.dict(os.environ, {"ATC_MCP_LEVERS": "confirm"}):
            server = m.McpServer(self.ck, "http")
        args = {"skill": "review", "session": "idle-id"}
        ctx = {"modern": True, "caps": {"elicitation": {"form": {}}}, "client": {"name": "fixture"}}
        question = server.call_tool({"name": "atc_run_skill", "arguments": args}, ctx, "other-id")
        self.assertEqual(question["resultType"], "input_required")
        self.terminals.act.assert_not_called()
        answer = server.call_tool({"name": "atc_run_skill", "arguments": args,
                                   "requestState": question["requestState"],
                                   "inputResponses": {"confirm": {"action": "accept", "content": {"confirm": True}}}},
                                  ctx, "other-id")
        self.assertFalse(answer["isError"])
        self.terminals.act.assert_called_once_with("/dev/fake", "type", "/review")
        readonly = m.McpServer(self.ck, "http", levers=False)
        result = readonly.call_tool({"name": "atc_tools", "arguments": {}}, ctx, "other-id")
        self.assertFalse(result["isError"])
        self.assertIn("notes", result["content"][0]["text"])
        refused = readonly.call_tool({"name": "atc_run_skill", "arguments": args}, ctx, "other-id")
        self.assertTrue(refused["isError"])

    def test_restart_stops_workers_as_well_as_launcher_and_rechecks_children(self):
        m = self.m
        self.fleet.procs[12] = {"ppid": 11, "etime": "00:30", "command": "node /opt/example/worker.js", "rss": 1024}
        self.model["tools"] = self.fleet.tools.model(self.fleet.procs, list(self.fleet.sessions.values()), self.now)
        lever = m.tool_restart_server(self.ck, {"server": "notes"}, m.Call())
        with mock.patch.object(m, "process_table", return_value=self.fleet.procs), mock.patch.object(m.os, "kill") as kill:
            self.assertTrue(lever.run()[0])
            self.assertEqual(kill.call_args_list, [mock.call(12, m.signal.SIGTERM), mock.call(11, m.signal.SIGTERM)])
            kill.reset_mock()
            self.fleet.procs[12]["command"] = "unrelated"
            self.assertFalse(lever.run()[0])
            kill.assert_not_called()

    def test_model_and_http_read_views_contain_only_safe_tools_data(self):
        m = self.m
        service = m.Service(self.fleet, self.quota, [], self.terminals, background=False,
                            claude_cmd=[str(self.fx.bin)], route_cmd=None)
        service.model, service.at = self.model, self.now
        self.assertIn("tools", service.web_model())
        status, data = service.get("/api/fleet")
        self.assertEqual(status, 200)
        self.assertEqual(data["tools"], self.model["tools"])
        self.assertEqual(data["tools"]["servers"][0]["copies"], 1)
        for data in (self.model["tools"], service.web_model()):
            out = json.dumps(data)
            self.assertNotIn(self.fx.secret, out)
            self.assertNotIn("Authorization", out)
            self.assertNotIn("/opt/example/notes.js", out)


if __name__ == "__main__":
    unittest.main()
