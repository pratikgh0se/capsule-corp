# mutation-check: targets-predate-range b957739c9 -- names out of tests, no behaviour pinned ::
#   RunbookSnippetBehaviour.test_the_named_hook_runs_clean_within_the_snippets_own_timeout
"""M6 (t_ae574709): the RUNBOOK's `~/.claude/settings.json` hook snippet for
`bin/capsule-unblocked-hook.sh` is the exact JSON the owner pastes, so it must work pasted as-is:
Claude Code does not shell-expand a hook `command`, so every path is absolute (no `$REPO`, no
`~`, no env var), each hook carries `timeout: 10`, and each event runs its own mode.

Z2, two oracles: the snippet's own shape (parsed out of `docs/RUNBOOK.md`, never re-typed), and
a real run of the hook script that snippet names, with the snippet's exact argv and timeout,
against a cwd outside every configured repo (so it prints nothing and exits 0 - the hook's own
documented silent path).
"""
import json
import os
import re
import shlex
import subprocess
import unittest
from pathlib import Path

from capsule import paths
from capsule.tests.public_tree import private_only

RUNBOOK = paths.REPO / "docs" / "RUNBOOK.md"
HOOK = "bin/capsule-unblocked-hook.sh"
MODE_FOR_EVENT = {"SessionStart": "session-start", "Stop": "stop"}


def snippet_text(runbook_text):
    """The one fenced json block in `runbook_text` that wires `capsule-unblocked-hook.sh`."""
    blocks = [b for b in re.findall(r"```json\n(.*?)```", runbook_text, re.S) if "capsule-unblocked-hook" in b]
    if len(blocks) != 1:
        raise AssertionError(f"expected exactly one json block naming the hook, found {len(blocks)}")
    return blocks[0]


def hook_commands(runbook_text):
    """[(event, command string, timeout)] for every hook in the snippet."""
    doc = json.loads(snippet_text(runbook_text))
    return [(event, h["command"], h.get("timeout"))
            for event, groups in doc["hooks"].items() for g in groups for h in g["hooks"]]


class RunbookSnippetShape(unittest.TestCase):
    def setUp(self):
        self.commands = hook_commands(RUNBOOK.read_text())

    @private_only
    def test_it_wires_exactly_session_start_and_stop(self):
        self.assertEqual(sorted(e for e, _, _ in self.commands), ["SessionStart", "Stop"])

    @private_only
    def test_every_command_is_an_absolute_path_with_no_expansion(self):
        for event, command, _ in self.commands:
            with self.subTest(event=event):
                script = shlex.split(command)[0]
                self.assertTrue(os.path.isabs(script), command)
                self.assertTrue(script.endswith("/" + HOOK), command)
                for bad in ("$", "~", "`"):
                    self.assertNotIn(bad, command)

    @private_only
    def test_every_hook_has_timeout_10(self):
        for event, _, timeout in self.commands:
            with self.subTest(event=event):
                self.assertEqual(timeout, 10)
                self.assertIs(type(timeout), int)

    @private_only
    def test_each_event_runs_its_own_mode(self):
        for event, command, _ in self.commands:
            with self.subTest(event=event):
                self.assertEqual(shlex.split(command)[1:], [MODE_FOR_EVENT[event]])

    @private_only
    def test_the_snippet_section_no_longer_tells_the_reader_to_substitute_a_variable(self):
        """Only this section: `$REPO` is legitimate shorthand elsewhere in the RUNBOOK (shell
        examples a human runs in their own shell, where it does expand)."""
        text = RUNBOOK.read_text()
        start = text.index("Add to `~/.claude/settings.json`")
        section = text[start:text.index("\nTests: ", start)]
        self.assertIn("capsule-unblocked-hook", section)
        self.assertNotIn("$REPO", section)


class RunbookSnippetBehaviour(unittest.TestCase):
    @private_only
    def test_the_named_hook_runs_clean_within_the_snippets_own_timeout(self):
        """Run each snippet command for real, swapping only this checkout's root in for the
        absolute prefix (the worktree is not at the owner's path), under the snippet's own
        timeout."""
        for event, command, timeout in hook_commands(RUNBOOK.read_text()):
            with self.subTest(event=event):
                argv = shlex.split(command)
                argv[0] = str(paths.REPO / HOOK)
                self.assertTrue(os.access(argv[0], os.X_OK), argv[0])
                env = {k: v for k, v in os.environ.items() if k != "CAPSULE_WORKER"}
                env["CAPSULE_UNBLOCKED_REPOS"] = "/nonexistent-repo"    # cwd outside it: silent path
                done = subprocess.run(argv, input=json.dumps({"cwd": "/tmp", "stop_hook_active": False}),
                                      capture_output=True, text=True, timeout=timeout, env=env)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(done.stdout, "")


class SnippetFinder(unittest.TestCase):
    """The extractor itself must fail loud, never silently pass on a moved snippet."""

    def test_no_block_or_two_blocks_is_an_error(self):
        with self.assertRaises(AssertionError):
            snippet_text("no snippet here")
        two = "```json\n{\"capsule-unblocked-hook\": 1}\n```\n```json\n{\"capsule-unblocked-hook\": 2}\n```"
        with self.assertRaises(AssertionError):
            snippet_text(two)


class IntentExamples(unittest.TestCase):
    """Z1: every `contracts/intent/runbook_hook_snippet.jsonl` example judged by the same
    extractor over the document it names."""

    def test_every_example_matches_its_oracle(self):
        lines = (paths.INTENT / "runbook_hook_snippet.jsonl").read_text().splitlines()
        self.assertTrue(lines)
        for line in lines:
            row = json.loads(line)
            snippet = "```json\n" + json.dumps(row["given"]["document"]["settings"]) + "\n```"
            cmds = hook_commands(snippet)
            ok = all(os.path.isabs(shlex.split(c)[0]) and "$" not in c and t == 10 for _, c, t in cmds)
            self.assertEqual(ok, row["expect"]["usable_as_pasted"], row["id"])


if __name__ == "__main__":
    unittest.main()
