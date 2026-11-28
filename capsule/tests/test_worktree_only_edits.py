"""PreToolUse hook for Edit/Write/NotebookEdit (card t_f47a8f51, row 557). Z2: every case
asserts exit code AND stderr. Runs `bin/worktree-only-edits.py` as a real subprocess against
crafted stdin JSON - Claude Code's own hook interface, never imported and called directly."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from capsule import intent, paths

HOOK = paths.REPO / "bin" / "worktree-only-edits.py"
# A temp directory stands in for the main checkout so the hook's "configured repo does not
# exist" warning never fires (the real host path is absent in the worker container).
HOST_ROOT = "/home/example/CodeBase/capsule-corp"
_TMP = tempfile.TemporaryDirectory()
CAPSULE_ROOT = os.path.join(os.path.realpath(_TMP.name), "capsule-corp")
os.mkdir(CAPSULE_ROOT)
MIXED_ROOT = CAPSULE_ROOT.replace("capsule-corp", "Capsule-Corp")
SAMPLE_TOOL_ROOT = "/home/example/CodeBase/other-app"


def run_hook(payload, repos=CAPSULE_ROOT):
    """Runs the real script as a subprocess with `payload` (a dict, or the raw string to use
    verbatim for the malformed-input cases) on stdin. Returns (returncode, stderr)."""
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    env = {**os.environ, "CAPSULE_WORKTREE_REPOS": repos}
    proc = subprocess.run([sys.executable, str(HOOK)], input=stdin, capture_output=True,
                           text=True, timeout=10, env=env)
    return proc.returncode, proc.stderr


def edit(path, agent_id="a1", key="file_path"):
    payload = {"tool_name": "Edit" if key == "file_path" else "NotebookEdit",
               "tool_input": {key: path}}
    if agent_id is not None:
        payload["agent_id"] = agent_id
    return payload


# (case id, path, key, agent_id, want_rc, a lowercase fragment stderr must contain when
# refused (I4's resolved-path oracle), or None when allowed (stderr must be empty)).
CASES = [
    ("subagent-sample-tool-allowed", f"{SAMPLE_TOOL_ROOT}/scripts/x.sh", "file_path", "a1", 0,
     None),
    ("subagent-repo-root-itself-refused", CAPSULE_ROOT, "file_path", "a1", 2, "capsule-corp"),
    ("subagent-scratchpad-allowed", f"{CAPSULE_ROOT}/scratchpad/notes.md", "file_path", "a1", 0, None),
    ("subagent-dot-claude-refused (I2)", f"{CAPSULE_ROOT}/.claude/settings.local.json",
     "file_path", "a1", 2, "settings.local.json"),
    ("subagent-outside-allowed", "/home/example/CodeBase/other/a.md", "file_path", "a1", 0, None),
    ("subagent-dotdot-escape-refused",
     f"{CAPSULE_ROOT}/.worktrees/worktree-edits/../../capsule/tick.py",
     "file_path", "a1", 2, "tick.py"),
    ("subagent-lowercase-apfs-escape-refused (I1)", f"{CAPSULE_ROOT.lower()}/capsule/tick.py",
     "file_path", "a1", 2, "tick.py"),
    ("subagent-mixed-case-repo-name-escape-refused (I1)", f"{MIXED_ROOT}/capsule/tick.py",
     "file_path", "a1", 2, "tick.py"),
    ("subagent-firmlink-escape-refused (I1)", f"/System/Volumes/Data{CAPSULE_ROOT}/capsule/tick.py", "file_path", "a1", 2, "tick.py"),
    ("subagent-firmlink-lowercase-refused (R2-I1)",
     f"/system/volumes/data{CAPSULE_ROOT.lower()}/capsule/tick.py",
     "file_path", "a1", 2, "tick.py"),
    ("subagent-notebook-main-refused", f"{CAPSULE_ROOT}/notes.ipynb", "notebook_path", "a1", 2, "notes.ipynb"),
    ("subagent-notebook-worktree-allowed",
     f"{CAPSULE_ROOT}/.worktrees/worktree-edits/notes.ipynb", "notebook_path", "a1", 0, None),
    ("main-thread-sample-tool-main-allowed (C1)",
     f"{SAMPLE_TOOL_ROOT}/scripts/mutation_check.sh", "file_path", None, 0, None),
]


def tearDownModule():
    _TMP.cleanup()


class Cases(unittest.TestCase):
    def test_table(self):
        for case_id, path, key, agent_id, want_rc, fragment in CASES:
            with self.subTest(case_id):
                rc, err = run_hook(edit(path, agent_id=agent_id, key=key))
                self.assertEqual(rc, want_rc, err)
                if fragment is None:
                    self.assertEqual(err, "")
                else:
                    self.assertTrue(err.startswith("Blocked by"), err)
                    self.assertIn(fragment, err.lower())


class SymlinkEscape(unittest.TestCase):
    def test_symlink_escape_refused(self):
        # A symlink built in a fresh temp dir (outside both repos) whose target is a file in
        # the main checkout: refused, naming the real target, not the requested link path (I4).
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "escape.py"
            os.symlink(Path(CAPSULE_ROOT) / "capsule" / "tick.py", link)
            rc, err = run_hook(edit(str(link)))
            self.assertEqual(rc, 2)
            self.assertTrue(err.startswith("Blocked by"), err)
            self.assertIn("tick.py", err.lower())


class MalformedInput(unittest.TestCase):
    MALFORMED = [
        ("", "malformed"),
        ("not json at all", "malformed"),
        (json.dumps({"tool_name": "Edit"}), "malformed"),
        (json.dumps({"tool_name": "Edit", "tool_input": {}, "agent_id": "a1"}), "no file_path"),
        (json.dumps({"tool_name": "Edit", "agent_id": "a1",
                      "tool_input": {"file_path": f"{CAPSULE_ROOT}/capsule/tick.py\x00x"}}), "malformed"),
    ]

    def test_malformed_or_incomplete_input_allowed_with_a_specific_warning(self):
        for stdin, fragment in self.MALFORMED:
            with self.subTest(stdin[:40]):
                rc, err = run_hook(stdin)
                self.assertEqual(rc, 0, err)
                self.assertIn(fragment, err.lower())


class ConfiguredRepos(unittest.TestCase):
    """The guarded repos come from CAPSULE_WORKTREE_REPOS (colon list), not a fixed list."""

    def test_missing_configured_repo_warns_and_is_still_guarded(self):
        gone = os.path.join(os.path.dirname(CAPSULE_ROOT), "no-such-repo")
        rc, err = run_hook(edit(f"{gone}/a.py"), repos=f"{CAPSULE_ROOT}:{gone}")
        self.assertEqual(rc, 2)
        self.assertIn(f"configured repo '{gone}' does not exist", err)
        self.assertIn("Blocked by", err)

    def test_relative_entry_ignored_with_a_warning(self):
        rc, err = run_hook(edit("/home/example/CodeBase/other/a.md"), repos="relative/dir")
        self.assertEqual(rc, 0, err)
        self.assertIn("ignoring 'relative/dir'", err)

    def test_a_repo_given_only_by_env_is_guarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.realpath(tmp)
            rc, err = run_hook(edit(f"{root}/x.py"), repos=root)
            self.assertEqual(rc, 2)
            self.assertIn("x.py", err.lower())
            rc, err = run_hook(edit(f"{CAPSULE_ROOT}/x.py"), repos=root)
            self.assertEqual((rc, err), (0, ""))


class IntentExamples(unittest.TestCase):
    """Z1/Z2, I3: shipped examples are real hook payloads, run through the real script."""

    def test_every_example_matches_the_real_script(self):
        examples = intent.load("worktree_only_edits")
        self.assertGreaterEqual(len(examples), 3)
        for ex in examples:
            with self.subTest(ex["id"]):
                raw = json.dumps(ex["given"]["document"]).replace(HOST_ROOT, CAPSULE_ROOT)
                doc = json.loads(raw)
                rc, err = run_hook(doc)
                self.assertEqual(rc, ex["expect"]["exit"], err)
                want = ex["expect"]["stderr_has"]
                self.assertEqual(err, "") if want == "" else self.assertIn(want, err)


if __name__ == "__main__":
    unittest.main()
