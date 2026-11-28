# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
#   TestText.test_statusline_names_boards_whose_loop_is_not_alive
"""The Claude Code chat plugin's helper (claude/capsule-chat/scripts/capsule_chat.py): the status
line and the read-only commands. Fake runner throughout, so no test reaches a real board.
Two oracles (Z2): golden status-line text for hand-written snapshots, and a read-only check that
every CLI call the helper makes is a `status` call."""
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from capsule import paths

sys.path.insert(0, str(paths.REPO / "claude" / "capsule-chat" / "scripts"))
import capsule_chat as cc  # noqa: E402

NOW = 1_800_000_000
SNAP = {
    "generated_at": NOW,
    "boards": [{"board": "capsule-corp", "loop": "alive", "auto_landing": True}],
    "running": [{"name": "A", "number": 1}, {"name": "B", "number": None}],
    "needs_you": [{"board": "capsule-corp", "number": 27, "name": "Slack routing",
                   "ask": "Two reviewers disagree.", "since": NOW - 3 * 3600}],
    "blocked": [{"name": "C", "reason": "parked on purpose"}] * 5,
    "attention": {"open": [{"what": "x"}], "handled_count": 0},
}


class FakeRunner:
    """Stands in for subprocess.run; records every call."""

    def __init__(self, stdout="", returncode=0, error=None):
        self.stdout, self.returncode, self.error, self.calls = stdout, returncode, error, []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd[3:])      # [python, -m, capsule, <args...>]
        if self.error:
            raise self.error
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, "")


class Repo(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        (self.repo / "capsule").mkdir(parents=True)
        (self.repo / "capsule" / "__main__.py").write_text("")
        (self.repo / "capsule" / "statusview.py").write_text("")
        (self.repo / ".claude-plugin").mkdir()
        (self.repo / ".claude-plugin" / "marketplace.json").write_text('{"name": "capsule-corp"}')
        self.env = {"XDG_CACHE_HOME": str(self.root / "cache"), "CAPSULE_REPO": str(self.repo)}

    def run_main(self, verb, runner, stdin="", env=None):
        out = io.StringIO()
        code = cc.main([verb], stdin=io.StringIO(stdin), out=out, env=env or self.env,
                       runner=runner)
        return code, out.getvalue()


class TestText(unittest.TestCase):
    def test_statusline_golden_alive(self):
        self.assertEqual(cc._statusline_text(SNAP),
                         "🟢 ▶️ 2 running · 🙋 1 need you · 🧱 5 blocked · 🔴 1 problems")

    def test_statusline_names_boards_whose_loop_is_not_alive(self):
        snap = dict(SNAP, boards=[{"board": "capsule-corp", "loop": "down"},
                                  {"board": "sample-app", "loop": "alive"},
                                  {"board": "other", "loop": "stalled"}])
        self.assertEqual(cc._statusline_text(snap),
                         "🔴 loop down: capsule-corp, other · ▶️ 2 running · 🙋 1 need you"
                         " · 🧱 5 blocked · 🔴 1 problems")

    def test_needs_you_golden(self):
        self.assertEqual(cc._needs_you_text(SNAP),
                         "🙋 NEEDS YOU (1)\n🙋 Slack routing #27 · Two reviewers disagree. · 3h")

    def test_needs_you_empty_and_loop_down_note(self):
        snap = dict(SNAP, needs_you=[], boards=[{"board": "capsule-corp", "loop": "down"}])
        self.assertEqual(cc._needs_you_text(snap),
                         "Nothing needs you right now.\n"
                         "(loop down on capsule-corp; this list may be out of date)")

    def test_untrusted_text_loses_escape_and_control_characters(self):
        snap = dict(SNAP, needs_you=[{"name": "Bad\x1b[31mname\r", "number": None,
                                      "ask": "a\x07b\nc", "since": None}])
        text = cc._needs_you_text(snap)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)
        self.assertEqual(text.count("\n"), 1)       # header + one item, the ask's newline gone


class TestFindRepo(Repo):
    def test_env_wins(self):
        self.assertEqual(cc._find_repo("/", self.env), self.repo)

    def test_walks_up_from_a_subdirectory(self):
        sub = self.repo / "capsule" / "deep"
        sub.mkdir()
        self.assertEqual(cc._find_repo(str(sub), {}), self.repo.resolve())

    def test_none_when_nowhere(self):
        self.assertIsNone(cc._find_repo(self.root, {}))
        self.assertIsNone(cc._find_repo(self.root, {"CAPSULE_REPO": str(self.root / "nope")}))

    def test_wrong_env_does_not_fall_back_to_the_session_dir(self):
        env = {"CAPSULE_REPO": str(self.root / "nope")}
        self.assertIsNone(cc._find_repo(str(self.repo), env))

    def test_main_only_folder_is_not_accepted(self):
        other = self.root / "other"
        (other / "capsule").mkdir(parents=True)
        (other / "capsule" / "__main__.py").write_text("")
        self.assertIsNone(cc._find_repo(str(other), {}))

    def test_wrong_marketplace_name_is_not_accepted(self):
        (self.repo / ".claude-plugin" / "marketplace.json").write_text('{"name": "else"}')
        self.assertIsNone(cc._find_repo(str(self.repo), {}))

    def test_nul_in_session_dir_gives_the_quiet_line(self):
        code, out = self.run_main("statusline", FakeRunner(), env={},
                                  stdin=json.dumps({"cwd": "/tmp/a\x00b"}))
        self.assertEqual((code, out), (0, "🔴 capsule: no answer\n"))


class TestStatusline(Repo):
    def test_prints_one_line(self):
        code, out = self.run_main("statusline", FakeRunner(json.dumps(SNAP)))
        self.assertEqual(code, 0)
        self.assertEqual(out, "🟢 ▶️ 2 running · 🙋 1 need you · 🧱 5 blocked · 🔴 1 problems\n")

    def test_capsule_not_found(self):
        code, out = self.run_main("statusline", FakeRunner(),
                                  env={"XDG_CACHE_HOME": self.env["XDG_CACHE_HOME"]},
                                  stdin='{"cwd": "%s"}' % self.root)
        self.assertEqual((code, out), (0, "🔴 capsule: not found (set CAPSULE_REPO)\n"))

    def test_session_cwd_from_stdin_finds_the_repo(self):
        runner = FakeRunner(json.dumps(SNAP))
        stdin = json.dumps({"workspace": {"current_dir": str(self.repo)}})
        code, out = self.run_main("statusline", runner,
                                  env={"XDG_CACHE_HOME": self.env["XDG_CACHE_HOME"]}, stdin=stdin)
        self.assertTrue(out.startswith("🟢"))

    def test_capsule_down_or_broken_never_raises(self):
        for runner in (FakeRunner(returncode=1), FakeRunner(error=OSError("no python")),
                       FakeRunner(error=subprocess.TimeoutExpired("x", 8)),
                       FakeRunner("not json"), FakeRunner("[]"), FakeRunner('{"boards": 3}')):
            code, out = self.run_main("statusline", runner)
            self.assertEqual((code, out), (0, "🔴 capsule: no answer\n"))
            self.assertEqual(len(out.splitlines()), 1)

    def test_bad_stdin_is_ignored(self):
        code, out = self.run_main("statusline", FakeRunner(json.dumps(SNAP)), stdin="{{{")
        self.assertTrue(out.startswith("🟢"))

    def test_cache_reuses_a_fresh_copy_and_refreshes_a_stale_one(self):
        runner = FakeRunner(json.dumps(SNAP))
        cc.snapshot(self.repo, self.env, runner)
        cc.snapshot(self.repo, self.env, runner)
        self.assertEqual(len(runner.calls), 1)
        cache = cc._cache_file(self.repo, self.env)
        self.assertEqual(oct(cache.parent.stat().st_mode & 0o777), "0o700")
        cc.snapshot(self.repo, self.env, runner, now=cache.stat().st_mtime + cc.CACHE_TTL_S + 1)
        self.assertEqual(len(runner.calls), 2)

    def test_unwritable_cache_still_answers(self):
        env = dict(self.env, XDG_CACHE_HOME="/dev/null/nope")
        self.assertEqual(cc.snapshot(self.repo, env, FakeRunner(json.dumps(SNAP)))["generated_at"],
                         NOW)

    def test_cache_is_per_capsule_home(self):
        a = cc._cache_file(self.repo, dict(self.env, CAPSULE_HOME="/a"))
        b = cc._cache_file(self.repo, dict(self.env, CAPSULE_HOME="/b"))
        self.assertNotEqual(a, b)


class TestCommands(Repo):
    def test_status_passes_the_screen_through(self):
        runner = FakeRunner("the screen\nline two\n")
        code, out = self.run_main("status", runner)
        self.assertEqual((code, out), (0, "the screen\nline two\n"))

    def test_needs_you_is_never_cached(self):
        runner = FakeRunner(json.dumps(SNAP))
        self.run_main("needs-you", runner)
        self.run_main("needs-you", runner)
        self.assertEqual(len(runner.calls), 2)

    def test_failure_is_a_plain_message_with_exit_zero(self):
        for verb in ("status", "needs-you"):
            code, out = self.run_main(verb, FakeRunner(returncode=2))
            self.assertEqual((code, out), (0, cc.UNAVAILABLE + "\n"))

    def test_unknown_verb_prints_usage_and_exits_zero(self):
        code, out = self.run_main("pause", FakeRunner())
        self.assertEqual(code, 0)
        self.assertIn("usage", out)

    def test_read_only_every_call_is_a_status_call(self):
        runner = FakeRunner(json.dumps(SNAP))
        for verb in ("statusline", "status", "needs-you", "pause", "unpark", "human"):
            self.run_main(verb, runner)
        self.assertTrue(runner.calls)
        self.assertTrue(all(call[0] == "status" for call in runner.calls), runner.calls)

    def test_no_internal_ids_in_output(self):
        for verb in ("statusline", "needs-you"):
            _, out = self.run_main(verb, FakeRunner(json.dumps(SNAP)))
            self.assertNotRegex(out, r"t_[0-9a-f]{8}|inc_|python3 -m")


class TestPluginFiles(unittest.TestCase):
    root = paths.REPO / "claude" / "capsule-chat"

    def test_manifest_names_the_plugin(self):
        doc = json.loads((self.root / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(doc["name"], "capsule-chat")

    def test_marketplace_points_at_the_plugin(self):
        doc = json.loads((paths.REPO / ".claude-plugin" / "marketplace.json").read_text())
        src = doc["plugins"][0]["source"]
        self.assertTrue((paths.REPO / src / ".claude-plugin" / "plugin.json").is_file())

    def test_commands_are_read_only_and_user_invoked(self):
        for verb in ("status", "needs-you"):
            text = (self.root / "skills" / verb / "SKILL.md").read_text()
            self.assertIn("disable-model-invocation: true", text)
            self.assertIn(f"capsule_chat.py {verb})", text)    # allowed-tools: this verb only
            self.assertEqual(text.count("allowed-tools:"), 1)


if __name__ == "__main__":
    unittest.main()
