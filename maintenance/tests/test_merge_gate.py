# mutation-check: targets-predate-range b957739c9 -- fixture rename only, no behaviour pinned ::
#   StateDir.test_default_is_outside_every_mounted_tree
import os
import sys
import json
import unittest
import unittest.mock
import tempfile
from pathlib import Path
os.environ["AGENT_OPS_NO_SEND"] = "1"
sys.path[:0] = [str(Path(__file__).resolve().parents[n]) for n in (1, 2)]
import capsule.tests  # noqa: E402,F401 - its __init__ installs the swept TMPDIR (tmpclean)
os.environ.setdefault("EVENTS_SPOOL", tempfile.mkdtemp(prefix="events-gate-") + "/events.jsonl")
# Never the real host state dir: approval stamps, locks and suite worktrees all live here.
os.environ["MERGE_GATE_STATE"] = tempfile.mkdtemp(prefix="gate-state-")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import board
import merge_gate as mg
# Never the real host boards dir: an isolated, empty dir keeps every call below from ever reading
# the live kanban tree, and patching board.BOARDS_DIR means a call site that forgets to pass
# boards_dir is isolated by construction too. Scoped to this
# module's own test run (setUpModule/tearDownModule) - `board` is shared process-
# wide across every test file `unittest discover` loads, and a bare module-level
# assignment here would leak into every other test module's default.
NO_BOARDS = Path(tempfile.mkdtemp(prefix="gate-boards-"))
_ORIG_BOARDS_DIR = None
# The suite's default image resolves like the runner's (pin, else the recorded image): pin one.
_PIN = unittest.mock.patch.dict(os.environ, {"OPS_WORKER_IMAGE": "capsule-python:test-pin"})


def setUpModule():
    global _ORIG_BOARDS_DIR
    _ORIG_BOARDS_DIR = board.BOARDS_DIR
    board.BOARDS_DIR = NO_BOARDS
    _PIN.start()


def tearDownModule():
    board.BOARDS_DIR = _ORIG_BOARDS_DIR
    _PIN.stop()


class StateDir(unittest.TestCase):
    def test_default_is_outside_every_mounted_tree(self):
        d = str(mg.DEFAULT_STATE)
        self.assertTrue(d.endswith("/.local/state/capsule-corp/merge-gate"), d)
        for mounted in ("/CodeBase/", "/.config/capsule-corp", "second-brain"):
            self.assertNotIn(mounted, d)

    def test_env_override_wins_and_create_makes_it_0700(self):
        import stat
        tmp = Path(tempfile.mkdtemp(prefix="gate-sd-")) / "nested" / "state"
        old = os.environ["MERGE_GATE_STATE"]
        os.environ["MERGE_GATE_STATE"] = str(tmp)
        try:
            self.assertEqual(mg.state_dir(), tmp)
            self.assertFalse(tmp.exists())              # never created just by reading
            mg.state_dir(create=True)
            self.assertEqual(stat.S_IMODE(tmp.stat().st_mode), 0o700)
        finally:
            os.environ["MERGE_GATE_STATE"] = old


class DefaultStateDirGuard(unittest.TestCase):
    """Mirrors board_view._refuse_default_vault_under_test (board_view.py:162):
    under AGENT_OPS_NO_SEND (the suite's own marker, set at import above) a
    call to `state_dir()` with no MERGE_GATE_STATE must never fall through to
    the real `~/.local/state/capsule-corp/merge-gate` - it must refuse. Approval
    stamps, `board.lock_path` and the gate worktrees all resolve through this
    one function, so the guard protects all three at once."""

    def test_without_the_marker_the_default_state_dir_is_used(self):
        """Production shape: no AGENT_OPS_NO_SEND, no MERGE_GATE_STATE -> the
        module's real default (DEFAULT_STATE, pinned by this test to a temp
        dir) is returned."""
        import pathlib
        from unittest import mock
        tmp = pathlib.Path(tempfile.mkdtemp())
        old_state = os.environ.pop("MERGE_GATE_STATE", None)
        try:
            with mock.patch.dict(os.environ, {"AGENT_OPS_NO_SEND": ""}), \
                 mock.patch.object(mg, "DEFAULT_STATE", tmp):
                os.environ.pop("AGENT_OPS_NO_SEND", None)
                self.assertEqual(mg.state_dir(), tmp)
        finally:
            if old_state is not None:
                os.environ["MERGE_GATE_STATE"] = old_state

    def test_under_the_marker_with_no_override_state_dir_refuses(self):
        old_state = os.environ.pop("MERGE_GATE_STATE", None)
        try:
            with self.assertRaises(RuntimeError) as cm:
                mg.state_dir()
            self.assertIn("MERGE_GATE_STATE", str(cm.exception))
        finally:
            if old_state is not None:
                os.environ["MERGE_GATE_STATE"] = old_state

    def test_an_explicit_override_is_never_refused(self):
        tmp = Path(tempfile.mkdtemp(prefix="gate-sd-guard-"))
        old_state = os.environ.get("MERGE_GATE_STATE")
        os.environ["MERGE_GATE_STATE"] = str(tmp)
        try:
            self.assertEqual(mg.state_dir(), tmp)
        finally:
            if old_state is None:
                os.environ.pop("MERGE_GATE_STATE", None)
            else:
                os.environ["MERGE_GATE_STATE"] = old_state


class ReadHelper(unittest.TestCase):
    def test_rc_and_empty_raise(self):
        with self.assertRaises(mg.InfraError):
            mg.read(lambda a, cwd=None, timeout=None: (1, "connection refused"), ["gh", "pr", "view"])
        with self.assertRaises(mg.InfraError):
            mg.read(lambda a, cwd=None, timeout=None: (0, "  "), ["gh", "pr", "view"])
        self.assertEqual(mg.read(lambda a, cwd=None, timeout=None: (0, "  "), ["gh"], required=False), "")


class TestCmd(unittest.TestCase):
    def test_default_and_override(self):
        self.assertEqual(mg.test_cmd_for("my-app", {}), "python3 -m unittest discover tests")
        self.assertEqual(mg.test_cmd_for("capsule-corp", {"OPS_TEST_CMD_capsule_corp": "python3 -m unittest discover maintenance/tests"}),
                         "python3 -m unittest discover maintenance/tests")


class SpecStampPermissions(unittest.TestCase):
    """C0 item 6 / threat model 4.2: an approval stamp is an approval. The dir
    is 0700 and the stamp 0600, the same rule bin/approve-spec.sh applies."""

    def test_the_stamp_and_its_dir_are_owner_only(self):
        path = mg.write_spec_approval("chmod-probe-slug", via="test")
        self.assertEqual(oct(path.stat().st_mode & 0o777), oct(0o600))
        self.assertEqual(oct(path.parent.stat().st_mode & 0o777), oct(0o700))


    def test_a_pre_existing_loose_dir_is_tightened(self):
        path = mg.spec_approval_path("chmod-probe-two")
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o755)
        mg.write_spec_approval("chmod-probe-two", via="test")
        self.assertEqual(oct(path.parent.stat().st_mode & 0o777), oct(0o700))


class RunInCloneStaleWarning(unittest.TestCase):
    def test_failed_remote_refs_fetch_prepends_a_warning(self):
        """The clone then keeps repo_root's own refs/remotes/origin/*, so
        diff-cover's --compare-branch may resolve against a stale base (#17)."""
        def fake_default_sh(argv, cwd=None, timeout=None):
            if "+refs/remotes/origin/*:refs/remotes/origin/*" in argv:
                return 1, "fetch failed"
            if argv[:2] == ["git", "rev-parse"]:
                return 0, "a" * 40 + "\n"
            return 0, ""
        with unittest.mock.patch.object(mg, "_default_sh", fake_default_sh):
            rc, out = mg.run_in_clone("/repo", "HEAD", "true", sh=lambda *a, **k: (0, "Coverage: 90%\n"))
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("warning: compare-branch may be stale\n"))
        self.assertIn("Coverage: 90%", out)

    def test_clean_run_has_no_warning(self):
        def fake_default_sh(argv, cwd=None, timeout=None):
            return (0, "a" * 40 + "\n") if argv[:2] == ["git", "rev-parse"] else (0, "")
        with unittest.mock.patch.object(mg, "_default_sh", fake_default_sh):
            rc, out = mg.run_in_clone("/repo", "HEAD", "true", sh=lambda *a, **k: (0, "ok\n"))
        self.assertEqual((rc, out), (0, "ok\n"))


class RunInClone(unittest.TestCase):
    def test_docker_argv_carries_the_deps_cache_mount_and_env_and_still_network_none(self):
        """A5 (row 217, design doc section 2c): the same -v <cache>:/deps:ro +
        offline pip/npm -e flags every sandboxed profile's docker_env carries
        must ride along on the gate's own offline suite re-run - and
        docker_network none must never be relaxed to get them there."""
        import deps
        def fake_default_sh(argv, cwd=None, timeout=None):
            return (0, "a" * 40 + "\n") if argv[:2] == ["git", "rev-parse"] else (0, "")
        calls = []
        def sh(argv, cwd=None, timeout=None):
            calls.append(argv)
            return (0, "ok\n")
        with unittest.mock.patch.object(mg, "_default_sh", fake_default_sh), \
             unittest.mock.patch.dict(os.environ, {"AGENT_OPS_DEPS_CACHE": "/tmp/depcache-x"}):
            mg.run_in_clone("/repo", "HEAD", "true", sh=sh)
            flags = deps.docker_flags()
        docker_calls = [c for c in calls if c[:2] == ["docker", "run"]]
        self.assertEqual(len(docker_calls), 1)
        argv = docker_calls[0]
        self.assertIn("--network", argv); self.assertEqual(argv[argv.index("--network") + 1], "none")
        n = len(flags)
        self.assertTrue(any(argv[i:i + n] == flags for i in range(len(argv) - n + 1)),
                        f"docker_flags() {flags} not found as a contiguous run in {argv}")


class SpecApprovalStamp(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mg-spec-"))
        self._old = os.environ.get("MERGE_GATE_STATE")
        os.environ["MERGE_GATE_STATE"] = str(self.root)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("MERGE_GATE_STATE", None)
        else:
            os.environ["MERGE_GATE_STATE"] = self._old

    def test_path_is_under_the_gate_state_root(self):
        p = mg.spec_approval_path("retention-sweep")
        self.assertEqual(p, self.root / "approvals" / "spec" / "retention-sweep.json")

    def test_bad_slug_is_refused(self):
        for bad in ("../etc/passwd", "Retention", "", "a/b"):
            with self.assertRaises(ValueError):
                mg.spec_approval_path(bad)

    def test_missing_stamp_reads_none(self):
        self.assertIsNone(mg.read_spec_approval("nope"))

    def test_write_then_read(self):
        mg.write_spec_approval("one", via="telegram", now=1757260800.0)
        got = mg.read_spec_approval("one")
        self.assertEqual(got, {"ts": 1757260800.0, "via": "telegram"})

    def test_write_twice_is_the_same_verdict(self):
        mg.write_spec_approval("one", now=1.0)
        mg.write_spec_approval("one", now=2.0)
        self.assertEqual(mg.read_spec_approval("one")["ts"], 2.0)
        self.assertEqual(len(list((self.root / "approvals" / "spec").glob("*"))), 1)

    def test_garbage_and_zero_ts_read_none(self):
        p = mg.spec_approval_path("one")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("not json", encoding="utf-8")
        self.assertIsNone(mg.read_spec_approval("one"))
        p.write_text('{"ts": 0}', encoding="utf-8")
        self.assertIsNone(mg.read_spec_approval("one"))

    def test_the_cli_writes_the_stamp(self):
        specs = self.root / "Specs"
        specs.mkdir()
        (specs / "one.md").write_text("---\ntitle: x\n---\n", encoding="utf-8")
        self.assertEqual(mg.main(["approve-spec", "one", "--specs-dir", str(specs)]), 0)
        self.assertIsNotNone(mg.read_spec_approval("one"))

    def test_the_cli_refuses_a_slug_with_no_spec_note(self):
        specs = self.root / "Specs"
        specs.mkdir()
        self.assertEqual(mg.main(["approve-spec", "ghost", "--specs-dir", str(specs)]), 1)
        self.assertIsNone(mg.read_spec_approval("ghost"))

    def test_the_cli_defaults_specs_dir_to_the_real_vault_env_var(self):
        # bin/approve-spec.sh never passes --specs-dir, so production always
        # goes through this default - it must follow the vault env var,
        # not a hardcoded default notes vault path (2026-09-07 review fix 1).
        vault = Path(tempfile.mkdtemp(prefix="mg-vault-"))
        (vault / "Specs").mkdir()
        (vault / "Specs" / "two.md").write_text("---\ntitle: y\n---\n", encoding="utf-8")
        old_vault = os.environ.get("OBSIDIAN_VAULT_PATH")
        os.environ["OBSIDIAN_VAULT_PATH"] = str(vault)
        try:
            self.assertEqual(mg.main(["approve-spec", "two"]), 0)
            self.assertIsNotNone(mg.read_spec_approval("two"))
            self.assertEqual(mg.main(["approve-spec", "ghost"]), 1)
            self.assertIsNone(mg.read_spec_approval("ghost"))
        finally:
            if old_vault is None:
                os.environ.pop("OBSIDIAN_VAULT_PATH", None)
            else:
                os.environ["OBSIDIAN_VAULT_PATH"] = old_vault


# --- C3/M042: the gate reads the host-only verdict store (row 127, task 4) ----


class ReleaseStamp(unittest.TestCase):
    """C10/M230: the third stamp. Cloned from the spec stamp (merge_gate.py:76-131)
    and carrying the same forgery argument - ~/.local/state is outside every tree
    a worker container mounts."""

    def tearDown(self):
        for p in (mg.state_dir() / "approvals" / "release").glob("*.json"):
            p.unlink()

    def test_tag_re_accepts_semver_with_a_leading_v(self):
        for good in ("v0.1.0", "v1.0.0", "v10.20.30", "v1.0.0-rc.1", "v0.0.1-alpha.2"):
            self.assertTrue(mg.TAG_RE.fullmatch(good), good)
        for bad in ("0.1.0", "v1.0", "v1", "v01.0.0", "v1.0.0+meta", "../etc",
                    "v1.0.0/../x", "", "V1.0.0", "v1.0.0 "):
            self.assertFalse(mg.TAG_RE.fullmatch(bad), bad)

    def test_path_validates_both_halves_before_joining(self):
        p = mg.release_approval_path("my-app", "v0.1.0")
        self.assertEqual(p.name, "my-app-v0.1.0.json")
        self.assertEqual(p.parent.name, "release")
        self.assertEqual(p.parent.parent.name, "approvals")
        for repo in ("../etc", "My-app", "a/b", "", "-lead"):
            with self.assertRaises(ValueError):
                mg.release_approval_path(repo, "v0.1.0")
        for tag in ("../etc", "0.1.0", "", "v1.0"):
            with self.assertRaises(ValueError):
                mg.release_approval_path("my-app", tag)

    def test_write_is_atomic_and_0600_under_0700_dirs(self):
        p = mg.write_release_approval("my-app", "v0.1.0", via="telegram", now=1000.0)
        self.assertEqual(oct(p.stat().st_mode)[-3:], "600")
        self.assertEqual(oct(p.parent.stat().st_mode)[-3:], "700")
        self.assertEqual(oct(p.parent.parent.stat().st_mode)[-3:], "700")
        self.assertEqual(json.loads(p.read_text())["via"], "telegram")
        self.assertEqual([q for q in p.parent.iterdir() if q.name.startswith(".")], [])

    def test_presence_is_the_whole_verdict(self):
        self.assertIsNone(mg.read_release_approval("my-app", "v0.1.0"))
        mg.write_release_approval("my-app", "v0.1.0", now=1000.0)
        # an OLD stamp still counts - the property that separates a release
        # stamp from a PR stamp, which must be newer than the last `gate:` comment
        self.assertEqual(mg.read_release_approval("my-app", "v0.1.0")["ts"], 1000.0)
        mg.write_release_approval("my-app", "v0.1.0", now=2000.0)  # a second approval refreshes it
        self.assertEqual(mg.read_release_approval("my-app", "v0.1.0")["ts"], 2000.0)

    def test_bad_stamp_files_read_as_absent(self):
        p = mg.release_approval_path("my-app", "v0.1.0")
        p.parent.mkdir(parents=True, exist_ok=True)
        for bad in ("not json", "[]", '{"ts": "x"}', '{"ts": 0}', '{"ts": -5}', "{}"):
            p.write_text(bad, encoding="utf-8")
            self.assertIsNone(mg.read_release_approval("my-app", "v0.1.0"), bad)

    def test_approve_release_verb_writes_the_stamp_and_refuses_a_bad_tag(self):
        self.assertEqual(mg.main(["approve-release", "my-app", "v0.2.0"]), 0)
        self.assertIsNotNone(mg.read_release_approval("my-app", "v0.2.0"))
        self.assertEqual(mg.main(["approve-release", "my-app", "0.2.0"]), 1)
        self.assertEqual(mg.main(["approve-release", "../etc", "v0.2.0"]), 1)


class RetiredPipeline(unittest.TestCase):
    """D238: approval is the owner's GitHub merge of the epic PR; the per-card pipeline is gone."""

    def test_the_pipeline_names_are_gone(self):
        for name in ("run", "step", "_step", "check_readonly", "pr_head", "cancel_approval",
                     "_open_pr", "_sync_with_main", "read_approval_stamp", "approval_stamp_path",
                     "check_suite", "check_ci", "check_mutation", "check_quality", "_needs_ui",
                     "_comment", "_record_verdict", "_host_quality_pass", "_board", "_board_infra",
                     "_infra", "_verdict_env", "sh", "_PR_URL_RE"):
            self.assertFalse(hasattr(mg, name), name)

    def test_the_kept_helpers_are_still_there(self):
        for name in ("SLUG_RE", "TAG_RE", "state_dir", "read", "run_in_clone",
                     "write_spec_approval", "read_release_approval"):
            self.assertTrue(hasattr(mg, name), name)

    def test_the_retired_cli_verbs_are_refused_and_the_scripts_are_gone(self):
        for verb in ("run", "check", "pr-head", "cancel-approval"):
            with self.assertRaises(SystemExit) as ctx, unittest.mock.patch("sys.stderr"):
                mg.main([verb])
            self.assertEqual(ctx.exception.code, 2, verb)
        bindir = Path(__file__).resolve().parents[2] / "bin"
        for script in ("approve-pr.sh", "cancel-approval.sh"):
            self.assertFalse((bindir / script).exists(), script)


if __name__ == "__main__":
    unittest.main()
