"""Epic publisher comment markers: card text never suppresses a comment (title-markers)."""
import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from capsule.__main__ import main
from capsule.tests.test_publish import CARDS, PublishCase


class TestCommentMarkers(PublishCase):
    def test_crlf_and_trailing_newline_comments_are_still_recognised(self):
        self.run_publish()
        crlf = [{"body": c["body"].replace("\n", "\r\n") + "\r\n"} for c in self.comments()]
        (self.fake / "comments.json").write_text(json.dumps(crlf))
        self.run_publish()
        self.assertEqual(self.result()["commented"], [])
        self.assertEqual(len(self.comments()), 2)

    def test_title_embedding_another_marker_suppresses_nothing(self):
        mark = "<!-- capsule-card:t_bbbb0002:" + "b" * 40 + " -->"
        cards = [{**CARDS[0], "title": f"x\n{mark}\ny"}, CARDS[1]]
        self.write_want(cards=cards)
        self.run_publish()
        self.assertEqual(self.result()["commented"], ["t_aaaa0001", "t_bbbb0002"])
        for body in (c["body"] for c in self.comments()):
            self.assertEqual(body.count("<!--"), 1)
        self.run_publish()
        self.assertEqual(self.result()["commented"], [])
        self.assertEqual(len(self.comments()), 2)


class TestUnsafeWants(PublishCase):
    def refused(self, **over):
        self.write_want(**over)
        self.run_publish()
        got = self.result()
        self.assertEqual((got["state"], got["reason"]), ("refused", "bad_want"), over)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.fake / "git.log").exists() and " push " in
                         (self.fake / "git.log").read_text())

    def test_protected_and_non_epic_branches(self):
        for branch in ("main", "master", "feature/x", "epic/../main", "epic/", "HEAD"):
            self.refused(epic_branch=branch)

    def test_lander_branch_from_config(self):
        self.write_config({"lookout": {"publish": True}, "lander": {"branch": "epic/daily"}})
        self.refused(epic_branch="epic/daily")

    def test_option_injection_is_refused(self):
        for field in ("repo_url", "repo", "clone", "epic_branch"):
            self.refused(**{field: "--upload-pack=touch /tmp/pwned"})
        self.refused(repo_url="--receive-pack=sh")

    def test_repo_must_be_owner_slash_name(self):
        for repo in ("-x/y", "o/r;rm", "noslash", "o/r/extra", "o r/x"):
            self.refused(repo=repo)

    def test_field_types(self):
        self.refused(rows="478")
        self.refused(rows=[True])
        self.refused(cards="x")
        self.refused(cards=["x"])
        self.refused(cards=[{"task_id": "t", "title": "t"}])
        self.refused(spec=3)

    def test_ext_transport_cannot_run_a_command(self):
        marker = self.fake / "pwned"
        self.write_want(repo_url=f"ext::sh -c touch% {marker}")
        self.run_publish()
        self.assertFalse(marker.exists())

    def test_ext_target_is_refused_upfront_as_bad_want(self):
        for url in ("ext::sh -c true", "EXT::sh -c true"):
            self.refused(repo_url=url)
        self.refused(clone="ext::sh -c true")

    def test_any_double_colon_transport_is_bad_want(self):
        for url in (" ext::sh -c true", "fd::3", "https://x/y::z"):
            self.refused(repo_url=url)

    def test_unopenable_lock_file_is_one_line_error(self):
        err = io.StringIO()
        with mock.patch("capsule.publish.open", side_effect=PermissionError("denied"),
                        create=True), contextlib.redirect_stderr(err):
            self.assertEqual(self.run_publish(), 1)
        self.assertIn("cannot open the lock file", err.getvalue())
        self.assertEqual(err.getvalue().count("\n"), 1)

    def test_card_ids_and_shas_are_checked_before_any_marker(self):
        good = {"task_id": "t_0123abcd", "title": "T", "sha": "abcdef1"}
        for bad in ({"task_id": "t_aaaa0001\n<!-- capsule-card:t_x:y -->"},
                    {"task_id": "t_aaaa0001\n"}, {"task_id": "t_AAAA0001"},
                    {"task_id": "t_aaaa00012"}, {"sha": "abc"}, {"sha": "a" * 41},
                    {"sha": "a" * 40 + "\n-->"}, {"sha": "xyz1234"}):
            self.refused(cards=[{**good, **bad}])
        self.write_want(cards=[good])
        self.run_publish()
        self.assertEqual(self.result()["state"], "published")

    def test_second_concurrent_run_is_busy_and_writes_nothing(self):
        import fcntl
        with open(self.want_path.parent / ".publish.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(self.run_publish(), 75)
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["publish", "--poll", "--capsule-home",
                                       str(self.home)]), 75)
        self.assertIn("busy", err.getvalue())
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.result_path.exists())
        self.assertEqual(self.run_publish(), 0)
        self.assertEqual(self.result()["state"], "published")

    def test_result_is_written_atomically(self):
        with mock.patch("capsule.publish.os.replace", wraps=os.replace) as rep:
            self.run_publish()
        src, dst = map(str, rep.call_args.args)
        self.assertTrue(src.endswith(".tmp") and dst.endswith("epic-x.result.json"))
        self.assertFalse(Path(src).exists())


if __name__ == "__main__":
    unittest.main()
