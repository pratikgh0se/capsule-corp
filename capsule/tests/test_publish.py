"""LAND-10 (D4, row 478): `capsule publish` puts an epic on GitHub as one draft PR with one
comment per card. gh is a fake script that logs argv; the remote is a local bare repo."""
import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import config, publish
from capsule.__main__ import main

FAKE = r"""#!/usr/bin/env python3
import json, os, sys
d = os.environ["FAKE_DIR"]
def path(name): return os.path.join(d, name)
def has(name): return os.path.exists(path(name))
def load(name, dflt):
    return json.load(open(path(name))) if has(name) else dflt
def rev(b):
    b = b if isinstance(b, dict) else {"body": b, "login": "me"}
    return {"body": b["body"], "author": {"login": b["login"]}}
with open(path("argv.log"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
a = sys.argv[1:3]
if a == ["auth", "status"]:
    sys.exit(1 if has("no_auth") else 0)
if a == ["api", "user"]:
    print(load("login.json", "me"))
    sys.exit(1 if has("fail_api") else 0)
if a == ["repo", "view"]:
    sys.exit(1 if has("no_repo") else 0)
if a[0] == "pr" and has("fail_" + a[1]):
    sys.exit(1)
if a == ["pr", "list"]:
    print(json.dumps(load("prs.json", [])))
elif a == ["pr", "create"]:
    json.dump([{"number": 7, "url": "https://github.com/o/r/pull/7"}], open(path("prs.json"), "w"))
    json.dump(sys.stdin.read(), open(path("body.json"), "w"))
    print("https://github.com/o/r/pull/7")
elif a == ["pr", "view"] and has("view_list"):
    print("[]")
elif a == ["pr", "view"]:
    print(json.dumps({"isDraft": True, "comments": load("comments.json", []),
                      "reviews": [rev(b) for b in load("reviews.json", [])]}))
elif a == ["pr", "comment"]:
    got = load("comments.json", [])
    got.append({"body": sys.stdin.read()})
    json.dump(got, open(path("comments.json"), "w"))
"""
GITLEAKS = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_DIR/gitleaks.log"
prev=""
for a in "$@"; do
  [ "$prev" = "--config" ] && cat "$a" > "$FAKE_DIR/gitleaks.config"
  prev="$a"
done
[ -e "$FAKE_DIR/leak" ] && exit 3
[ -e "$FAKE_DIR/broken" ] && exit 2
exit 0
"""
CARDS = [{"task_id": "t_aaaa0001", "title": "First card", "sha": "a" * 40, "validator": "pass",
          "reviewer": "approve", "guide": "ok", "review_rounds": 2},
         {"task_id": "t_bbbb0002", "title": "Second card", "sha": "b" * 40}]
GIT_WRAP = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_DIR/git.log"
exec {real} "$@"
"""
ID = ("-c", "user.name=t", "-c", "user.email=t@t")


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


class PublishCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.home, self.fake = root / "home", root / "fake"
        self.remote, self.clone = root / "remote.git", root / "clone"
        for d in (self.home, self.fake):
            d.mkdir()
        (self.fake / "gh").write_text(FAKE)
        (self.fake / "gh").chmod(0o755 | stat.S_IXUSR)
        git("init", "-q", "--bare", str(self.remote))
        git("init", "-q", "-b", "main", str(self.clone))
        self.commit("one")
        (self.fake / "bin").mkdir()
        wrap = self.fake / "bin" / "git"
        wrap.write_text(GIT_WRAP.format(real=shutil.which("git")))
        wrap.chmod(0o755)
        (self.fake / "gitleaks").write_text(GITLEAKS)
        (self.fake / "gitleaks").chmod(0o755)
        env = {"CAPSULE_GH_BIN": str(self.fake / "gh"), "FAKE_DIR": str(self.fake),
               "CAPSULE_GITLEAKS_BIN": str(self.fake / "gitleaks"),
               "PATH": f"{self.fake / 'bin'}{os.pathsep}{os.environ['PATH']}"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.home / "lookout" / "publish").mkdir(parents=True)
        self.want_path = self.home / "lookout" / "publish" / "epic-x.want.json"
        self.result_path = self.want_path.with_name("epic-x.result.json")
        self.write_config({"lookout": {"publish": True}})
        self.write_want()

    def commit(self, name):
        (self.clone / name).write_text(name)
        git("add", name, cwd=self.clone)
        git(*ID, "commit", "-q", "-m", name, cwd=self.clone)

    def write_config(self, cfg):
        (self.home / "config.json").write_text(json.dumps(cfg))

    def write_want(self, **over):
        want = {"epic_branch": "epic/x", "clone": str(self.clone), "repo_url": str(self.remote),
                "repo": "o/r", "spec": "Specs/x.md", "rows": [478, 480], "cards": CARDS, **over}
        self.want_path.write_text(json.dumps(want))

    def run_publish(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return main(["publish", "--capsule-home", str(self.home)])

    def result(self):
        return json.loads(self.result_path.read_text())

    def calls(self):
        log = self.fake / "argv.log"
        return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []

    def remote_head(self):
        return git("--git-dir", str(self.remote), "rev-parse", "refs/heads/epic/x")

    def comments(self):
        return json.loads((self.fake / "comments.json").read_text())


class TestSwitch(PublishCase):
    def test_off_by_default_touches_nothing(self):
        for cfg in ({}, {"lookout": {"publish": False}}):
            self.write_config(cfg)
            self.assertEqual(self.run_publish(), 0)
        (self.home / "config.json").unlink()
        self.assertEqual(self.run_publish(), 0)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.result_path.exists())
        self.assertEqual(git("--git-dir", str(self.remote), "branch", "--list"), "")

    def test_bad_lookout_block_is_refused(self):
        for block in ({"publish": "yes"}, {"nope": 1}, []):
            with self.assertRaises(ValueError):
                config.validate_lookout(block)
        self.write_config({"lookout": {"publish": 1}})
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_publish(), 1)
        self.assertEqual(self.calls(), [])


class TestPreflight(PublishCase):
    def refused(self, reason):
        self.assertEqual(self.run_publish(), 0)
        got = self.result()
        self.assertEqual((got["state"], got["reason"]), ("refused", reason))
        self.assertNotIn("pr_url", got)
        self.assertFalse(self.remote_refs())
        self.assertFalse([c for c in self.calls() if c[:2] == ["pr", "create"]])

    def remote_refs(self):
        return git("--git-dir", str(self.remote), "branch", "--list")

    def test_auth_failure(self):
        (self.fake / "no_auth").write_text("")
        self.refused("gh_auth")
        self.assertEqual(self.calls()[0][:2], ["auth", "status"])
        self.assertEqual(len(self.calls()), 1)

    def test_repo_unknown(self):
        (self.fake / "no_repo").write_text("")
        self.refused("repo_unknown")
        self.assertEqual([c[:2] for c in self.calls()], [["auth", "status"], ["repo", "view"]])
        self.assertIn("o/r", self.calls()[1])

    def test_preflight_runs_before_push_in_order(self):
        self.run_publish()
        self.assertEqual([c[:2] for c in self.calls()][:2], [["auth", "status"], ["repo", "view"]])

    def test_bad_want(self):
        self.want_path.write_text("{not json")
        self.refused("bad_want")


class TestPublish(PublishCase):
    def test_first_run_pushes_opens_draft_pr_and_comments(self):
        self.assertEqual(self.run_publish(), 0)
        head = git("rev-parse", "HEAD", cwd=self.clone)
        self.assertEqual(self.remote_head(), head)
        self.assertEqual(self.result(), {
            "state": "published", "pr_url": "https://github.com/o/r/pull/7", "pr_number": 7,
            "head": head, "commented": ["t_aaaa0001", "t_bbbb0002"]})
        create = [c for c in self.calls() if c[:2] == ["pr", "create"]]
        self.assertEqual(len(create), 1)
        for part in ("--draft", "--base", "main", "--head", "epic/x"):
            self.assertIn(part, create[0])
        body = json.loads((self.fake / "body.json").read_text())
        for text in ("Specs/x.md", "478, 480", "- [ ] t_aaaa0001: First card"):
            self.assertIn(text, body)
        first = self.comments()[0]["body"]
        for text in ("t_aaaa0001: First card", "sha aaaaaaaa", "validator: pass",
                     "reviewer: approve", "review rounds: 2",
                     "<!-- capsule-card:t_aaaa0001:" + "a" * 40 + " -->"):
            self.assertIn(text, first)
        self.assertTrue(first.endswith("-->"))
        self.assertIn("guide: n/a", self.comments()[1]["body"])
        self.assertIn("review rounds: n/a", self.comments()[1]["body"])

    def test_gh_pr_list_and_push_argv(self):
        self.run_publish()
        listing = [c for c in self.calls() if c[:2] == ["pr", "list"]][0]
        self.assertIn("--head", listing)
        self.assertIn("number,url", listing)

    def test_rerun_never_double_posts(self):
        self.run_publish()
        self.assertEqual(self.run_publish(), 0)
        self.assertEqual(len(self.comments()), 2)
        self.assertEqual(len([c for c in self.calls() if c[:2] == ["pr", "create"]]), 1)
        self.assertEqual(self.result()["commented"], [])
        self.assertEqual(self.result()["pr_number"], 7)

    def test_new_card_after_rerun_is_the_only_comment(self):
        self.run_publish()
        extra = {"task_id": "t_cccc0003", "title": "Third", "sha": "c" * 40}
        self.write_want(cards=[*CARDS, extra])
        self.commit("two")
        self.run_publish()
        self.assertEqual(self.result()["commented"], ["t_cccc0003"])
        self.assertEqual(len(self.comments()), 3)

    def test_fast_forward_push_succeeds(self):
        self.run_publish()
        self.commit("two")
        self.run_publish()
        self.assertEqual(self.remote_head(), git("rev-parse", "HEAD", cwd=self.clone))
        self.assertEqual(self.result()["state"], "published")

    def test_diverged_refuses_and_pushes_nothing(self):
        self.run_publish()
        old = self.remote_head()
        git("reset", "-q", "--hard", "HEAD~0", cwd=self.clone)
        git("checkout", "-q", "--orphan", "other", cwd=self.clone)
        self.commit("elsewhere")
        (self.fake / "argv.log").unlink()
        self.run_publish()
        got = self.result()
        self.assertEqual((got["state"], got["reason"], got["detail"], got["pr_number"]),
                         ("refused", "diverged", "git merge-base", 7))
        self.assertEqual(self.remote_head(), old)
        self.assertFalse([c for c in self.calls() if c[:2] in (["pr", "create"],
                                                                ["pr", "comment"])])

    def test_push_failure_is_refused(self):
        self.write_want(repo_url=str(self.remote) + "-missing")
        self.run_publish()
        self.assertEqual(self.result()["reason"], "push_failed")
        self.assertFalse([c for c in self.calls() if c[:2] == ["pr", "create"]])

    def test_never_merges_forces_or_touches_main(self):
        self.run_publish()
        for call in self.calls():
            self.assertNotIn("merge", call)
        self.assertNotIn("refs/heads/main", git("--git-dir", str(self.remote), "for-each-ref"))
        pushes = [x for x in (self.fake / "git.log").read_text().splitlines() if " push " in x]
        self.assertEqual(len(pushes), 1)
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        self.assertTrue(pushes[0].endswith(f"push -- {self.remote} {sha}:refs/heads/epic/x"))
        for flag in ("--force", "-f", "--mirror", "--delete"):
            self.assertNotIn(flag, pushes[0].split())

    def test_pr_list_failure_is_pr_failed_and_creates_nothing(self):
        (self.fake / "fail_list").write_text("")
        self.run_publish()
        self.assertEqual(self.result()["reason"], "pr_failed")
        self.assertEqual(self.result()["detail"], "gh pr list")
        self.assertFalse([c for c in self.calls() if c[:2] == ["pr", "create"]])

    def test_pr_view_failure_is_not_published(self):
        (self.fake / "fail_view").write_text("")
        self.run_publish()
        self.assertEqual((self.result()["state"], self.result()["reason"]),
                         ("refused", "pr_failed"))
        self.assertFalse([c for c in self.calls() if c[:2] == ["pr", "comment"]])

    def test_comment_failure_is_refused_and_keeps_what_was_posted(self):
        self.run_publish()
        extra = {"task_id": "t_cccc0003", "title": "Third", "sha": "c" * 40}
        self.write_want(cards=[*CARDS, extra])
        (self.fake / "fail_comment").write_text("")
        self.run_publish()
        self.assertEqual((self.result()["reason"], self.result()["detail"]),
                         ("pr_failed", "gh pr comment"))
        self.assertEqual(len(self.comments()), 2)

    def test_bad_want_does_not_stop_the_next_want(self):
        bad = self.want_path.with_name("a-bad.want.json")
        bad.write_text(json.dumps({"epic_branch": 5}))
        self.run_publish()
        self.assertEqual(self.result_path.with_name("a-bad.result.json").exists(), True)
        self.assertEqual(json.loads(
            bad.with_name("a-bad.result.json").read_text())["reason"], "bad_want")
        self.assertEqual(self.result()["state"], "published")

    def test_wrong_shaped_pr_view_is_pr_failed(self):
        (self.fake / "view_list").write_text("")
        self.run_publish()
        self.assertEqual((self.result()["reason"], self.result()["detail"]),
                         ("pr_failed", "gh pr view"))

    def test_recursion_bomb_want_does_not_stop_the_next(self):
        bomb = self.want_path.with_name("a-bomb.want.json")
        bomb.write_text("[" * 200000)
        self.run_publish()
        self.assertEqual(json.loads(bomb.with_name("a-bomb.result.json").read_text())["reason"],
                         "internal")
        self.assertEqual(self.result()["state"], "published")

    def test_non_object_config_is_refused(self):
        (self.home / "config.json").write_text("[]")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_publish(), 1)
        self.assertEqual(self.calls(), [])

    def test_ship_key_is_allowed_in_lookout_block(self):
        self.assertIsNone(config.validate_lookout({"ship": "yes", "publish": False}))

    def test_crash_in_one_want_is_contained(self):
        with mock.patch("capsule.publish._publish", side_effect=[OSError("x"), {"state": "ok"}]):
            self.want_path.with_name("a.want.json").write_text(self.want_path.read_text())
            self.run_publish()
        first = json.loads(self.want_path.with_name("a.result.json").read_text())
        self.assertEqual((first["reason"], first["detail"]), ("internal", "OSError"))
        self.assertEqual(self.result(), {"state": "ok"})


if __name__ == "__main__":
    unittest.main()
