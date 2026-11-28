# mutation-check: targets-predate-range ff625e94 --
#   test-only stronger assertion, no product change ::
#   Orchestrator.test_cli_exit_code_is_2_while_the_flag_is_off
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

os.environ["AGENT_OPS_NO_SEND"] = "1"
os.environ.setdefault("EVENTS_SPOOL", tempfile.mkdtemp(prefix="events-release-") + "/events.jsonl")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import events                # noqa: E402
import merge_gate            # noqa: E402
import merge_gate as mg      # noqa: E402
import release_gate as rg    # noqa: E402

_MODULE_ENV = None


def setUpModule():
    """Own temp state dir for this module, whatever an earlier module restored or removed.
    OPS_RELEASE_GATE is pinned OFF (verdicts.ops_env() is {**ops.env, **os.environ}, so this wins
    over the machine's ops.env); every "on" case passes env= explicitly."""
    global _MODULE_ENV
    _MODULE_ENV = unittest.mock.patch.dict(os.environ, {
        "MERGE_GATE_STATE": tempfile.mkdtemp(prefix="release-state-"),
        "AGENT_OPS_NO_SEND": "1", "OPS_RELEASE_GATE": "0"})
    _MODULE_ENV.start()


def tearDownModule():
    _MODULE_ENV.stop()


PROMOTED = """# Changelog

## Unreleased

## v0.1.0 - 2026-09-10

### Added
- Soft tick toggle on the timer page (t_b2cc669d)
"""

NOT_REINSERTED = """# Changelog

## v0.1.0 - 2026-09-10

### Added
- Soft tick toggle on the timer page (t_b2cc669d)
"""

REINSERTED_BUT_DIRTY = """# Changelog

## Unreleased

- Something already landed after the release (t_deadbeef)

## v0.1.0 - 2026-09-10

- Soft tick toggle (t_b2cc669d)
"""

NO_TASK_LINE = """# Changelog

## Unreleased

## v0.1.0 - 2026-09-10

### Added
- Soft tick toggle on the timer page
"""

BRACKETED = """# Changelog

## [Unreleased]

## [v0.1.0] - 2026-09-10

- Soft tick toggle (t_b2cc669d)
"""


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=str(repo), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null",
                        "GIT_CONFIG_SYSTEM": "/dev/null",
                        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


class Flag(unittest.TestCase):
    def test_off_by_default_and_a_typo_is_off(self):
        self.assertFalse(rg.enabled({}))
        self.assertFalse(rg.enabled({"OPS_RELEASE_GATE": "0"}))
        self.assertFalse(rg.enabled({"OPS_RELEASE_GATE": "nonsense"}))
        self.assertFalse(rg.enabled({"OPS_RELEASE_GATE": ""}))
        for on in ("1", "true", "TRUE", "yes"):
            self.assertTrue(rg.enabled({"OPS_RELEASE_GATE": on}), on)

    def test_version_of_strips_the_git_v(self):
        self.assertEqual(rg.version_of("v0.1.0"), "0.1.0")
        self.assertEqual(rg.version_of("v1.2.3-rc.1"), "1.2.3-rc.1")

    def test_release_dir_for_rejects_a_path_escape_in_either_argument(self):
        with self.assertRaises(ValueError):
            rg.release_dir_for("../x", "v0.1.0")
        with self.assertRaises(ValueError):
            rg.release_dir_for("my-app", "../x")


class Changelog(unittest.TestCase):
    def test_promoted_with_unreleased_reinserted(self):
        ok, why = rg.changelog_promoted(PROMOTED, "v0.1.0")
        self.assertTrue(ok, why)

    def test_bracket_form_is_accepted(self):
        ok, why = rg.changelog_promoted(BRACKETED, "v0.1.0")
        self.assertTrue(ok, why)

    def test_unreleased_not_reinserted_says_why_it_matters(self):
        ok, why = rg.changelog_promoted(NOT_REINSERTED, "v0.1.0")
        self.assertFalse(ok)
        self.assertIn("Unreleased", why)
        self.assertIn("quality gate", why)

    def test_unreleased_reinserted_but_not_empty_is_fine(self):
        """A card that merged after the promotion has already added its line -
        that is the pipeline working, not a broken release."""
        ok, why = rg.changelog_promoted(REINSERTED_BUT_DIRTY, "v0.1.0")
        self.assertTrue(ok, why)

    def test_no_tag_section(self):
        ok, why = rg.changelog_promoted(PROMOTED, "v0.2.0")
        self.assertFalse(ok)
        self.assertIn("v0.2.0", why)

    def test_tag_section_with_no_task_id_line(self):
        ok, why = rg.changelog_promoted(NO_TASK_LINE, "v0.1.0")
        self.assertFalse(ok)
        self.assertIn("t_", why)

    def test_missing_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            ok, why = rg.check_changelog(d, "v0.1.0")
            self.assertFalse(ok)
            self.assertIn("CHANGELOG.md", why)


class Tag(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.origin = root / "origin.git"
        self.repo = root / "work"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "first")
        _git(self.repo, "remote", "add", "origin", str(self.origin))
        _git(self.repo, "tag", "v0.1.0")
        _git(self.repo, "push", "-q", "origin", "HEAD", "--tags")

    def test_tag_on_origin_returns_its_sha(self):
        ok, why, sha = rg.check_tag(self.repo, "v0.1.0")
        self.assertTrue(ok, why)
        self.assertEqual(len(sha), 40)

    def test_tag_absent_from_origin(self):
        ok, why, sha = rg.check_tag(self.repo, "v9.9.9")
        self.assertFalse(ok)
        self.assertIn("v9.9.9", why)
        self.assertEqual(sha, "")

    def test_a_broken_git_is_an_infra_error_not_a_verdict(self):
        with self.assertRaises(mg.InfraError):
            rg.check_tag(self.repo, "v0.1.0", sh=lambda argv, cwd=None, timeout=None: (128, "fatal: boom"))

    def test_a_stalled_git_ls_remote_is_an_infra_error_not_a_hang(self):
        def sh(argv, cwd=None, timeout=None):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
        with self.assertRaises(mg.InfraError):
            rg.check_tag(self.repo, "v0.1.0", sh=sh)

    def test_an_annotated_tags_peeled_sha_wins_over_the_tag_object(self):
        tag_obj = "a" * 40
        commit_sha = "b" * 40

        def sh(argv, cwd=None, timeout=None):
            return (0, f"{tag_obj}\trefs/tags/v0.1.0\n{commit_sha}\trefs/tags/v0.1.0^{{}}\n")

        ok, why, sha = rg.check_tag(self.repo, "v0.1.0", sh=sh)
        self.assertTrue(ok, why)
        self.assertEqual(sha, commit_sha)


class Sbom(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.rel = Path(self.tmp.name) / "rel"; self.rel.mkdir()

    def test_existing_sbom_passes(self):
        (self.rel / "sbom.spdx.json").write_text(json.dumps({"spdxVersion": "SPDX-2.3"}))
        ok, why = rg.check_sbom(self.rel, self.tmp.name, "my-app", which=lambda n: None)
        self.assertTrue(ok, why)
        self.assertNotIn("skipped", why)

    def test_syft_generates_it(self):
        calls = []

        def sh(argv, cwd=None, timeout=None):
            calls.append(argv)
            return (0, json.dumps({"spdxVersion": "SPDX-2.3"}))

        ok, why = rg.check_sbom(self.rel, self.tmp.name, "my-app",
                                sh=sh, which=lambda n: "/usr/local/bin/syft")
        self.assertTrue(ok, why)
        self.assertTrue((self.rel / "sbom.spdx.json").exists())
        self.assertIn("syft", calls[0][0])

    def test_no_syft_is_a_skip_that_prints_the_one_liner(self):
        ok, why = rg.check_sbom(self.rel, self.tmp.name, "my-app", which=lambda n: None)
        self.assertTrue(ok, why)          # a missing tool is never a verdict
        self.assertTrue(why.startswith("sbom: skipped - "))
        self.assertIn("sbom.spdx.json", why)

    def test_unparseable_sbom_fails_closed(self):
        (self.rel / "sbom.spdx.json").write_text("not json")
        ok, why = rg.check_sbom(self.rel, self.tmp.name, "my-app", which=lambda n: None)
        self.assertFalse(ok)

    def test_syft_write_into_a_read_only_release_dir_is_a_verdict_not_a_traceback(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory write permission")
        os.chmod(self.rel, 0o500)
        self.addCleanup(os.chmod, self.rel, 0o700)

        def sh(argv, cwd=None, timeout=None):
            return (0, json.dumps({"spdxVersion": "SPDX-2.3"}))

        ok, why = rg.check_sbom(self.rel, self.tmp.name, "my-app",
                                sh=sh, which=lambda n: "/usr/local/bin/syft")
        self.assertFalse(ok)
        self.assertIn("sbom: cannot write", why)

    def test_the_whole_created_release_tree_is_0700_not_just_the_leaf(self):
        release_dir = Path(self.tmp.name) / "releases" / "my-app" / "v0.1.0"
        self.assertFalse(release_dir.exists())

        def sh(argv, cwd=None, timeout=None):
            return (0, json.dumps({"spdxVersion": "SPDX-2.3"}))

        ok, why = rg.check_sbom(release_dir, self.tmp.name, "my-app",
                                sh=sh, which=lambda n: "/usr/local/bin/syft")
        self.assertTrue(ok, why)
        for d in (release_dir, release_dir.parent, release_dir.parent.parent):
            self.assertEqual(oct(d.stat().st_mode)[-3:], "700", str(d))


class Artefact(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.rel = Path(self.tmp.name) / "rel"; self.rel.mkdir()

    def test_hashes_every_file_into_SHA256SUMS(self):
        (self.rel / "MyAppBar-0.1.0.zip").write_bytes(b"zipbytes")
        (self.rel / "sbom.spdx.json").write_text("{}")
        ok, why = rg.check_artefact(self.rel)
        self.assertTrue(ok, why)
        sums = (self.rel / "SHA256SUMS").read_text()
        self.assertIn(hashlib.sha256(b"zipbytes").hexdigest(), sums)
        self.assertNotIn("sbom.spdx.json", sums)      # metadata, not an artefact
        self.assertNotIn("SHA256SUMS", sums)
        self.assertEqual(oct((self.rel / "SHA256SUMS").stat().st_mode)[-3:], "600")

    def test_empty_directory_fails(self):
        ok, why = rg.check_artefact(self.rel)
        self.assertFalse(ok)
        self.assertIn("nothing", why)

    def test_missing_directory_fails_closed(self):
        ok, why = rg.check_artefact(self.rel / "nope")
        self.assertFalse(ok)

    def test_sums_are_sorted_and_rewritten_idempotently(self):
        (self.rel / "b.zip").write_bytes(b"b")
        (self.rel / "a.zip").write_bytes(b"a")
        rg.check_artefact(self.rel)
        first = (self.rel / "SHA256SUMS").read_text()
        rg.check_artefact(self.rel)
        self.assertEqual(first, (self.rel / "SHA256SUMS").read_text())
        self.assertLess(first.index("a.zip"), first.index("b.zip"))

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory write permission")
    def test_a_read_only_release_dir_is_a_verdict_not_a_traceback(self):
        (self.rel / "MyAppBar-0.1.0.zip").write_bytes(b"zipbytes")
        os.chmod(self.rel, 0o500)
        self.addCleanup(os.chmod, self.rel, 0o700)
        ok, why = rg.check_artefact(self.rel)
        self.assertFalse(ok)
        self.assertIn("artefact: cannot read", why)


class Stamp(unittest.TestCase):
    def test_presence_is_the_verdict(self):
        ok, why = rg.check_stamp("my-app", "v0.3.0")
        self.assertFalse(ok)
        self.assertIn("approve-release.sh", why)
        mg.write_release_approval("my-app", "v0.3.0", now=1000.0)
        self.addCleanup(mg.release_approval_path("my-app", "v0.3.0").unlink)
        ok, why = rg.check_stamp("my-app", "v0.3.0")
        self.assertTrue(ok, why)


class Orchestrator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.origin = root / "origin.git"
        self.repo = root / "work"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "CHANGELOG.md").write_text(PROMOTED, encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "first")
        _git(self.repo, "remote", "add", "origin", str(self.origin))
        _git(self.repo, "tag", "v0.1.0")
        _git(self.repo, "push", "-q", "origin", "HEAD", "--tags")
        self.rel = root / "rel"; self.rel.mkdir()
        (self.rel / "MyAppBar-0.1.0.zip").write_bytes(b"zipbytes")
        self.spool = str(root / "spool.jsonl")
        reg = root / "repos.json"   # sample registry: the release link comes from its url
        reg.write_text('[{"name": "my-app", "url": "https://example.com/acme/my-app.git"}]',
                       encoding="utf-8")
        patcher = unittest.mock.patch.dict(os.environ, {"AGENT_OPS_REPOS_JSON": str(reg)})
        patcher.start()
        self.addCleanup(patcher.stop)
        mg.write_release_approval("my-app", "v0.1.0", now=1000.0)
        self.addCleanup(
            lambda: mg.release_approval_path("my-app", "v0.1.0").unlink(missing_ok=True))

    def _check(self, **kw):
        kw.setdefault("env", {"OPS_RELEASE_GATE": "1"})
        kw.setdefault("repo_root", self.repo)
        kw.setdefault("release_dir", self.rel)
        kw.setdefault("which", lambda n: None)
        return rg.check("my-app", "v0.1.0", **kw)

    def _rows(self):
        return [r for r in events.read(spool=self.spool) if r["kind"] == "release"]

    def test_off_refuses_with_one_clear_line_and_emits_nothing(self):
        verdict, lines = rg.check("my-app", "v0.1.0", env={}, repo_root=self.repo,
                                  release_dir=self.rel, which=lambda n: None, spool=self.spool)
        self.assertEqual(verdict, "off")
        self.assertEqual(len(lines), 1)
        self.assertIn("OPS_RELEASE_GATE", lines[0])
        self.assertEqual(self._rows(), [])

    def test_all_five_pass_and_emit_exactly_one_release_event(self):
        verdict, lines = self._check(spool=self.spool)
        self.assertEqual(verdict, "released", lines)
        self.assertEqual(len(lines), 5)
        self.assertTrue(any(l.startswith("sbom: skipped - ") for l in lines))
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        p = rows[0]["payload"]
        self.assertEqual(p["repo"], "my-app")
        self.assertEqual(p["version"], "0.1.0")
        self.assertEqual(p["tag"], "v0.1.0")
        self.assertEqual(len(p["sha"]), 40)
        self.assertIn("releases/tag/v0.1.0", rows[0]["link"])

    def test_stops_at_the_first_failure_and_emits_nothing(self):
        (self.repo / "CHANGELOG.md").write_text(NOT_REINSERTED, encoding="utf-8")
        verdict, lines = self._check(spool=self.spool)
        self.assertEqual(verdict, "blocked:changelog")
        self.assertEqual(len(lines), 2)          # tag ok, changelog failed, nothing after
        self.assertEqual(self._rows(), [])

    def test_a_missing_stamp_blocks_last(self):
        mg.release_approval_path("my-app", "v0.1.0").unlink()
        verdict, lines = self._check(spool=self.spool)
        self.assertEqual(verdict, "blocked:stamp")
        self.assertEqual(self._rows(), [])

    def test_the_gate_never_merges_pushes_or_creates_a_release(self):
        calls = []

        def sh(argv, cwd=None, timeout=None):
            calls.append(list(argv))
            return merge_gate._default_sh(argv, cwd=cwd)

        self._check(sh=sh, spool=self.spool)
        flat = [" ".join(c) for c in calls]
        for forbidden in ("gh pr merge", "git push", "gh release create", "gh release upload"):
            self.assertFalse([f for f in flat if f.startswith(forbidden)], flat)

    def test_bad_args_block_before_any_filesystem_or_git_call(self):
        for repo, tag in (("my-app", "0.1.0"), ("../etc", "v0.1.0"), ("my-app", "v1.0")):
            verdict, lines = rg.check(repo, tag, env={"OPS_RELEASE_GATE": "1"},
                                      repo_root=self.repo, release_dir=self.rel,
                                      which=lambda n: None, spool=self.spool)
            self.assertEqual(verdict, "blocked:args", (repo, tag))
        self.assertEqual(self._rows(), [])

    def test_cli_exit_code_is_2_while_the_flag_is_off(self):
        """The suite pins OPS_RELEASE_GATE=0 in os.environ, which wins over
        ops.env inside verdicts.ops_env(), so `main` (which reads the real
        environment) is deterministic here and never releases anything."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = rg.main(["check", "my-app", "v0.1.0"])
        self.assertEqual(rc, 2)
        self.assertIn("release gate is off: set OPS_RELEASE_GATE=1", out.getvalue())

    def test_cli_exit_code_is_3_on_an_uncaught_oserror_not_a_traceback(self):
        """Defense in depth one level out: even a straggler OSError that
        neither check() nor a check_* function converted into a verdict must
        still exit 3, never print a Python traceback."""
        with unittest.mock.patch.object(rg, "check", side_effect=OSError("boom")):
            self.assertEqual(rg.main(["check", "my-app", "v0.1.0"]), 3)
