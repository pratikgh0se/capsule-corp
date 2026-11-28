"""maintenance/public_export.py (P10a). Fixture repos only; the real repo is never exported.

Z1 intent: two exports of one sha make no second commit; a planted personal path fails the export
before the commit and leaves DEST untouched; a de-listed file is deleted; --dry-run writes nothing.
Z2 oracle: expected results are fixed strings and counts, plus `git` itself reading DEST back.
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from capsule.tests.public_tree import private_only  # noqa: E402
import public_export  # noqa: E402
import public_lint  # noqa: E402
from public_export_fixture import PERSONAL, Fixture, git  # noqa: E402


class Export(Fixture):
    def test_exports_only_allowlisted_files_and_commits(self):
        rc, out, _ = self.export(self.sha1)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.names(), ["README.md", "capsule/a.py"])
        self.assertEqual(git(self.dest, "log", "--format=%s"), "Sync from private repo")
        self.assertEqual(git(self.dest, "rev-list", "--count", "HEAD"), "1")
        self.assertEqual(git(self.dest, "status", "--porcelain"), "")

    def test_two_exports_of_one_sha_make_no_second_commit(self):
        self.export(self.sha1)
        rc, out, _ = self.export(self.sha1)
        self.assertEqual(rc, 0)
        self.assertIn("no changes", out)
        self.assertEqual(git(self.dest, "rev-list", "--count", "HEAD"), "1")

    def test_delisted_file_is_deleted_and_changed_file_updated(self):
        self.export(self.sha1)
        self.put("release/public-paths.txt", "capsule/\n!capsule/private.py\n")
        self.put("capsule/a.py", "x = 2")
        sha2 = self.commit("two")
        rc, _, _ = self.export(sha2)
        self.assertEqual(rc, 0)
        self.assertEqual(self.names(), ["capsule/a.py"])
        self.assertEqual((self.dest / "capsule/a.py").read_text(), "x = 2\n")
        self.assertEqual(git(self.dest, "rev-list", "--count", "HEAD"), "2")

    def test_exports_the_given_sha_not_the_worktree(self):
        self.put("capsule/a.py", "x = 99")  # uncommitted
        self.export(self.sha1)
        self.assertEqual((self.dest / "capsule/a.py").read_text(), "x = 1\n")

    def test_executable_bit_survives(self):
        self.put("capsule/run.py", "print(1)", mode=0o755)
        sha = self.commit("exec")
        self.export(sha)
        self.assertTrue((self.dest / "capsule/run.py").stat().st_mode & 0o100)

    def test_no_commit_leaves_no_repo(self):
        rc, _, _ = self.export(self.sha1, "--no-commit")
        self.assertEqual(rc, 0)
        self.assertFalse((self.dest / ".git").exists())
        self.assertEqual(self.names(), ["README.md", "capsule/a.py"])


class ScanHook(Fixture):
    def test_planted_personal_path_fails_before_dest_is_touched(self):
        self.put("capsule/a.py", f'HOME = "{PERSONAL}"')
        sha = self.commit("bad")
        rc, out, err = self.export(sha)
        self.assertEqual(rc, 1)
        self.assertIn("capsule/a.py:1: personal-path", out)
        self.assertFalse(self.dest.exists())

    def test_hit_after_a_good_export_leaves_dest_as_it_was(self):
        self.export(self.sha1)
        self.put("capsule/a.py", f'HOME = "{PERSONAL}"')
        sha = self.commit("bad")
        rc, _, _ = self.export(sha)
        self.assertEqual(rc, 1)
        self.assertEqual((self.dest / "capsule/a.py").read_text(), "x = 1\n")
        self.assertEqual(git(self.dest, "rev-list", "--count", "HEAD"), "1")

    def test_personal_path_in_a_non_allowlisted_file_is_not_a_hit(self):
        rc, _, _ = self.export(self.sha1)  # docs/RUNBOOK.md holds PERSONAL but is not listed
        self.assertEqual(rc, 0)


class DryRun(Fixture):
    def test_dry_run_lists_counts_and_writes_nothing(self):
        rc, out, err = self.export(self.sha1, "--dry-run")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "public would change 2 files\n")
        self.assertIn("README.md\ncapsule/a.py", err)
        self.assertIn("(added 2, changed 0, deleted 0)", err)
        self.assertFalse(self.dest.exists())

    def test_dry_run_against_synced_dest_says_zero(self):
        self.export(self.sha1)
        _, out, _ = self.export(self.sha1, "--dry-run")
        self.assertIn("public would change 0 files", out)

    def test_dry_run_counts_change_and_delete(self):
        self.export(self.sha1)
        self.put("release/public-paths.txt", "capsule/\n!capsule/private.py\n")
        self.put("capsule/a.py", "x = 2")
        sha2 = self.commit("two")
        _, out, err = self.export(sha2, "--dry-run")
        self.assertEqual(out, "public would change 2 files\n")
        self.assertIn("(added 0, changed 1, deleted 1)", err)
        self.assertEqual(git(self.dest, "rev-list", "--count", "HEAD"), "1")

    def test_dry_run_reports_hits_and_exits_1(self):
        self.put("capsule/a.py", f'HOME = "{PERSONAL}"')
        sha = self.commit("bad")
        rc, out, _ = self.export(sha, "--dry-run")
        self.assertEqual(rc, 1)
        self.assertIn("personal-path", out)


class Identity(Fixture):
    """H2, L1, L2: the commit carries only the generic identity, no private sha, no hooks."""

    def test_identity_and_utc_ignore_user_config_and_env(self):
        gconf = self.root / "gconf"
        gconf.write_text("[user]\n\temail = real@private.example\n"
                         "[author]\n\temail = a@private.example\n"
                         "[committer]\n\temail = c@private.example\n")
        env = {"GIT_CONFIG_GLOBAL": str(gconf), "GIT_AUTHOR_EMAIL": "env@private.example",
               "GIT_COMMITTER_EMAIL": "env2@private.example", "GIT_AUTHOR_NAME": "Real Person"}
        with mock.patch.dict(os.environ, env):
            rc, _, _ = self.export(self.sha1)
        self.assertEqual(rc, 0)
        self.assertEqual(git(self.dest, "log", "-1", "--format=%an|%ae|%cn|%ce"),
                         "capsule-export|export@capsule.invalid|"
                         "capsule-export|export@capsule.invalid")
        self.assertTrue(git(self.dest, "log", "-1", "--format=%ai").endswith("+0000"))
        self.assertTrue(git(self.dest, "log", "-1", "--format=%ci").endswith("+0000"))

    def test_message_has_no_private_sha_and_mapping_log_has_it(self):
        self.export(self.sha1)
        self.assertNotIn(self.sha1[:8], git(self.dest, "log", "-1", "--format=%B"))
        public = git(self.dest, "rev-parse", "HEAD")
        line = (self.state / "public-export.log").read_text().splitlines()
        self.assertEqual(len(line), 1)
        self.assertEqual(line[0].split("\t")[1:], [public, self.sha1, "2"])
        self.export(self.sha1)  # no change: no new mapping line
        self.assertEqual(len((self.state / "public-export.log").read_text().splitlines()), 1)

    def test_dry_run_and_no_commit_write_no_mapping_line(self):
        self.export(self.sha1, "--dry-run")
        self.export(self.sha1, "--no-commit")
        self.assertFalse((self.state / "public-export.log").exists())

    def test_hooks_do_not_run_and_init_has_no_template_hooks(self):
        self.export(self.sha1)
        hooks = self.dest / ".git" / "hooks"
        self.assertEqual([p.name for p in hooks.glob("*")] if hooks.exists() else [], [])
        marker = self.root / "hook-ran"
        for name in ("pre-commit", "commit-msg", "post-commit"):
            hooks.mkdir(exist_ok=True)
            (hooks / name).write_text(f"#!/bin/sh\ntouch {marker}\n")
            (hooks / name).chmod(0o755)
        self.put("capsule/a.py", "x = 2")
        sha2 = self.commit("two")
        self.assertEqual(self.export(sha2)[0], 0)
        self.assertFalse(marker.exists())
        self.assertEqual(git(self.dest, "rev-list", "--count", "HEAD"), "2")


class GitEnvironment(Fixture):
    """M1, M3: redirecting git variables are ignored; the tree is built from raw blobs."""

    def test_git_dir_in_environment_does_not_redirect_anything(self):
        before = git(self.repo, "rev-parse", "HEAD")
        with mock.patch.dict(os.environ, {"GIT_DIR": str(self.repo / ".git"),
                                          "GIT_WORK_TREE": str(self.repo),
                                          "GIT_INDEX_FILE": str(self.root / "idx"),
                                          "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.name",
                                          "GIT_CONFIG_VALUE_0": "Real Person"}):
            rc, _, _ = self.export(self.sha1)
        self.assertEqual(rc, 0)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before)
        self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD"), "1")
        self.assertTrue((self.dest / ".git").is_dir())
        self.assertEqual(git(self.dest, "log", "-1", "--format=%an"), "capsule-export")
        self.assertEqual(self.names(), ["README.md", "capsule/a.py"])

    def test_clean_env_drops_every_redirecting_variable(self):
        names = ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                 "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
                 "GIT_CEILING_DIRECTORIES", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
                 "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_KEY_3"]
        with mock.patch.dict(os.environ, {n: "x" for n in names}):
            env = public_lint.clean_env()
        self.assertEqual([n for n in names if n in env], [])
        self.assertIn("PATH", env)

    def test_smudge_filter_is_not_applied(self):
        self.put(".gitattributes", "capsule/secret.txt filter=dec\n")
        self.put("capsule/secret.txt", "ENCRYPTED")
        sha = self.commit("filtered")
        git(self.repo, "config", "filter.dec.smudge", "sed s/ENCRYPTED/PLAINTEXT-SECRET/")
        self.assertEqual(self.export(sha)[0], 0)
        self.assertEqual((self.dest / "capsule/secret.txt").read_text(), "ENCRYPTED\n")

    def test_export_ignore_and_export_subst_attributes_are_not_applied(self):
        self.put(".gitattributes", "capsule/keep.txt export-ignore\ncapsule/id.txt export-subst\n")
        self.put("capsule/keep.txt", "kept")
        self.put("capsule/id.txt", "$Format:%H$")
        sha = self.commit("attrs")
        self.assertEqual(self.export(sha)[0], 0)
        self.assertEqual((self.dest / "capsule/keep.txt").read_text(), "kept\n")
        self.assertEqual((self.dest / "capsule/id.txt").read_text(), "$Format:%H$\n")


class Symlinks(Fixture):
    """M2, L5: absolute or escaping symlinks refuse the export; targets are scanned."""

    def link(self, name, target):
        p = self.repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, p)
        return self.commit("link")

    def test_absolute_target_fails(self):
        sha = self.link("capsule/l", PERSONAL)
        rc, out, _ = self.export(sha)
        self.assertEqual(rc, 1)
        self.assertIn("capsule/l:1: symlink: absolute target", out)
        self.assertIn("capsule/l:1: personal-path", out)  # the target text is scanned too
        self.assertFalse(self.dest.exists())

    def test_absolute_target_without_personal_text_fails(self):
        sha = self.link("capsule/l", "/etc/hosts")
        rc, out, _ = self.export(sha)
        self.assertEqual(rc, 1)
        self.assertIn("capsule/l:1: symlink: absolute", out)

    def test_target_leaving_the_tree_fails(self):
        for target in ("../../outside", "../../../x", "sub/../../../x"):
            with self.subTest(target=target):
                sha = self.link("capsule/l", target) if not (self.repo / "capsule/l").is_symlink() \
                    else self.relink(target)
                rc, out, _ = self.export(sha)
                self.assertEqual(rc, 1)
                self.assertIn("leaves the tree", out)

    def relink(self, target):
        (self.repo / "capsule/l").unlink()
        return self.link("capsule/l", target)

    def test_target_with_a_denylist_term_fails(self):
        sha = self.link("capsule/l", "acme-secret.txt")
        rc, out, _ = self.export(sha)
        self.assertEqual(rc, 1)
        self.assertIn("capsule/l:1: term:", out)

    def test_link_to_a_link_or_through_a_linked_dir_fails(self):
        self.link("capsule/l1", "a.py")
        sha = self.link("capsule/l2", "l1")
        rc, out, _ = self.export(sha)
        self.assertEqual(rc, 1)
        self.assertIn("capsule/l2:1: symlink: target passes through another symlink", out)
        self.assertNotIn("capsule/l1:", out)
        self.put("capsule/sub/f.py", "y = 1")
        self.link("capsule/d", "sub")
        sha = self.link("capsule/viad", "d/f.py")
        rc, out, _ = self.export(sha, "--dry-run")
        self.assertIn("capsule/viad:1: symlink: target passes through another symlink", out)

    def test_dangling_in_tree_link_is_fine(self):
        sha = self.link("capsule/l", "missing.py")
        self.assertEqual(self.export(sha)[0], 0)

    def test_in_tree_relative_symlink_is_exported_as_a_link(self):
        sha = self.link("capsule/l", "a.py")
        self.assertEqual(self.export(sha)[0], 0)
        self.assertTrue((self.dest / "capsule/l").is_symlink())
        self.assertEqual(os.readlink(self.dest / "capsule/l"), "a.py")
        self.assertEqual(self.export(sha, "--dry-run")[1].count("would change 0 files"), 1)

    def test_git_path_and_submodule_entries_fail(self):
        self.put("release/public-paths.txt", "capsule/\nREADME.md\nsub\n")
        git(self.repo, "add", "release/public-paths.txt")
        git(self.repo, "update-index", "--add", "--cacheinfo",
            f"160000,{self.sha1},sub")
        git(self.repo, "commit", "-q", "-m", "gitlink")
        sha = git(self.repo, "rev-parse", "HEAD")
        rc, out, _ = self.export(sha)
        self.assertEqual(rc, 1)
        self.assertIn("sub:1: not-a-blob", out)


class PrivateDenylist(Fixture):
    """L7: personal terms live in a private file; no file, no export."""

    def test_missing_denylist_refuses_and_touches_nothing(self):
        self.deny.unlink()
        rc, _, err = self.export(self.sha1)
        self.assertEqual(rc, 2)
        self.assertIn("fail closed", err)
        self.assertFalse(self.dest.exists())

    def test_denylist_term_in_an_allowlisted_file_fails(self):
        self.put("capsule/a.py", "# Acme-Secret was here")
        sha = self.commit("term")
        rc, out, _ = self.export(sha)
        self.assertEqual(rc, 1)
        self.assertIn("capsule/a.py:1: term:", out)
        self.assertFalse(self.dest.exists())

    def test_default_denylist_is_read_from_the_repo_being_exported(self):
        rc, _, err = self.export(self.sha1, deny=False)
        self.assertEqual(rc, 2)
        self.assertIn("fail closed", err)
        self.assertFalse(self.dest.exists())
        (self.repo / "release" / "private-denylist.txt").write_text("term\tacme-secret\n")
        self.assertEqual(self.export(self.sha1, deny=False)[0], 0)

    def test_malformed_denylist_refuses(self):
        self.deny.write_text("no-tab-here\n")
        rc, _, err = self.export(self.sha1)
        self.assertEqual(rc, 2)
        self.assertIn("want category", err)

    def test_line_exemption_and_path_exemption_load(self):
        self.deny.write_text("name\tacme-secret\tLICENSE\tREADME.md=author\n")
        self.put("LICENSE", "acme-secret")
        self.put("README.md", "author acme-secret\nacme-secret again")
        self.put("release/public-paths.txt", "capsule/\nREADME.md\nLICENSE\n")
        rc, out, _ = self.export(self.commit("exempt"), "--dry-run")
        self.assertEqual(rc, 1)
        self.assertIn("README.md:2: name:", out)
        self.assertNotIn("README.md:1:", out)
        self.assertNotIn("LICENSE:", out)

    def test_several_file_regex_fields_each_exempt_only_their_own_file(self):
        self.deny.write_text("name\tacme-secret\t\tA.md=alpha\tB.md=beta\n")
        self.put("A.md", "alpha acme-secret\nbeta acme-secret")
        self.put("B.md", "beta acme-secret\nalpha acme-secret")
        self.put("release/public-paths.txt", "capsule/\nA.md\nB.md\n")
        rc, out, _ = self.export(self.commit("two fields"), "--dry-run")
        self.assertEqual(rc, 1)
        self.assertIn("A.md:2: name:", out)
        self.assertIn("B.md:2: name:", out)
        self.assertNotIn("A.md:1:", out)
        self.assertNotIn("B.md:1:", out)

    def test_two_regex_fields_for_one_file_are_merged(self):
        self.deny.write_text("name\tacme-secret\t\tA.md=alpha\tA.md=beta\n")
        self.put("A.md", "alpha acme-secret\nbeta acme-secret\ngamma acme-secret")
        self.put("release/public-paths.txt", "capsule/\nA.md\n")
        rc, out, _ = self.export(self.commit("merged fields"), "--dry-run")
        self.assertEqual(rc, 1)
        self.assertIn("A.md:3: name:", out)
        self.assertNotIn("A.md:1:", out)
        self.assertNotIn("A.md:2:", out)

    def test_an_empty_or_malformed_file_regex_refuses(self):
        for bad in ("A.md=", "A.md=alpha|", "A.md", "=alpha", "A.md=(unclosed"):
            self.deny.write_text(f"name\tacme-secret\t\t{bad}\n")
            rc, _, err = self.export(self.sha1)
            self.assertEqual(rc, 2, bad)
            self.assertIn("FILE", err, bad)

    def test_public_lint_source_holds_only_generic_rules(self):
        cats = {c for c, _, _ in public_lint.RULES}
        self.assertEqual(cats, {"personal-path", "tailnet"})  # nothing personal, no vendor (D260)
        self.assertIn("personal-path", cats)
        src = Path(public_lint.__file__).read_text().lower()
        words = ("pra" + "tik", "pomo" + "doro", "obsi" + "dian", "second-" + "brain",
                 "herm" + "es")
        for word in words:
            self.assertNotIn(word, src)

    @private_only
    def test_the_real_allowlist_keeps_the_denylist_and_itself_private(self):
        allow = Path(public_lint.__file__).resolve().parents[1] / "release" / "public-paths.txt"
        lines = [ln.strip() for ln in allow.read_text().splitlines() if ln.strip()]
        inc = [public_lint.glob_re(ln) for ln in lines if not ln.startswith(("#", "!"))]
        exc = [public_lint.glob_re(ln[1:]) for ln in lines if ln.startswith("!")]
        for name in ("release/private-denylist.txt", "release/public-paths.txt"):
            public = any(r.match(name) for r in inc) and not any(r.match(name) for r in exc)
            self.assertFalse(public, name)
