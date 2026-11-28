"""LAND-10 slice 2: secret-scan preflight, whole-branch review plus ready, and `--poll`.
Same fakes as test_publish: a gh script that logs argv, a fake gitleaks, a local bare remote."""
import contextlib
import io
import json
import os
import time
import unittest
from unittest import mock

from capsule import publish
from capsule.__main__ import main
from capsule.tests import test_publish as base
from capsule.tests.test_publish import git

FAKE = base.FAKE.replace(
    'elif a == ["pr", "view"] and has("view_list"):',
    'elif a == ["pr", "view"] and "isDraft,reviews" in sys.argv:\n'
    '    print(json.dumps({**({} if has("no_isdraft") else {"isDraft": not has("is_ready")}),\n'
    '                      "reviews": [rev(b) for b in load("reviews.json", [])]}))\n'
    'elif a == ["pr", "view"] and has("view.json") and "comments" not in sys.argv:\n'
    '    print(open(path("view.json")).read())\n'
    'elif a == ["pr", "view"] and has("view_list"):') + """
if a == ["pr", "review"]:
    got = load("reviews.json", [])
    got.append(sys.stdin.read())
    json.dump(got, open(path("reviews.json"), "w"))
if a == ["pr", "ready"]:
    if "--undo" in sys.argv:
        if has("is_ready"):
            os.remove(path("is_ready"))
    else:
        open(path("is_ready"), "w").write("")
if a == ["pr", "edit"]:
    got = load("edits.json", [])
    got.append(sys.stdin.read())
    json.dump(got, open(path("edits.json"), "w"))
"""
BAD = "/Users/" + "someone/code/x"
TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


class S2Case(base.PublishCase):
    def setUp(self):
        super().setUp()
        (self.fake / "gh").write_text(FAKE)

    def run_poll(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return main(["publish", "--poll", "--capsule-home", str(self.home)])

    def reviews(self):
        return json.loads((self.fake / "reviews.json").read_text())

    def review_file(self, text=f"Looks good. See {BAD} and {TOKEN}\n"):
        self.want_path.with_name("epic-x.review.md").write_text(text)

    def names(self, *prefix):
        return [c for c in self.calls() if c[:len(prefix)] == list(prefix)]


class TestSecretScan(S2Case):
    def test_finding_refuses_before_push_without_secret_text(self):
        (self.fake / "leak").write_text("")
        self.assertEqual(self.run_publish(), 0)
        got = self.result()
        self.assertEqual((got["state"], got["reason"]), ("refused", "secret_found"))
        self.assertNotIn("pr_url", got)
        self.assertEqual(git("--git-dir", str(self.remote), "branch", "--list"), "")
        self.assertEqual([c[:2] for c in self.calls()], [["auth", "status"], ["repo", "view"]])

    def test_first_push_scans_all_of_head(self):
        self.run_publish()
        log = (self.fake / "gitleaks.log").read_text()
        self.assertIn(f"--log-opts {git('rev-parse', 'HEAD', cwd=self.clone)}\n", log)
        self.assertIn("--redact", log)
        self.assertIn("--exit-code 3", log)
        self.assertTrue(log.startswith(f"git {self.clone} --no-banner"), log)
        self.assertNotIn("detect", log)
        self.assertNotIn("--source", log)

    def test_rerun_scans_only_the_added_range(self):
        self.run_publish()
        old = git("rev-parse", "HEAD", cwd=self.clone)
        self.commit("two")
        self.run_publish()
        new = git("rev-parse", "HEAD", cwd=self.clone)
        self.assertIn(f"--log-opts {old}..{new}\n", (self.fake / "gitleaks.log").read_text())
        self.assertEqual(self.remote_head(), new)

    def test_missing_scanner_refuses_as_scan_failed(self):
        os.environ["CAPSULE_GITLEAKS_BIN"] = str(self.fake / "nope")
        self.run_publish()
        got = self.result()
        self.assertEqual((got["reason"], got["detail"]), ("secret_scan_failed", "gitleaks git"))
        self.assertEqual(git("--git-dir", str(self.remote), "branch", "--list"), "")

    def test_scanner_error_refuses_as_scan_failed(self):
        (self.fake / "broken").write_text("")
        self.run_publish()
        self.assertEqual(self.result()["reason"], "secret_scan_failed")
        self.assertEqual(git("--git-dir", str(self.remote), "branch", "--list"), "")

    def test_push_uses_scanned_sha_not_head(self):
        self.run_publish()
        pushes = [x for x in (self.fake / "git.log").read_text().splitlines() if " push " in x]
        sha = git("rev-parse", "HEAD", cwd=self.clone)
        self.assertTrue(pushes[0].endswith(f"{sha}:refs/heads/epic/x"))
        self.assertNotIn("+", pushes[0].split()[-1])

    def test_config_comes_from_the_remote_base_not_the_scanned_range(self):
        (self.clone / ".gitleaks.toml").write_text("# base config\n")
        git("add", ".gitleaks.toml", cwd=self.clone)
        git(*base.ID, "commit", "-q", "-m", "cfg", cwd=self.clone)
        self.run_publish()
        self.commit("two")
        (self.clone / ".gitleaks.toml").write_text("# allowlist my own secret\n")
        git("add", ".gitleaks.toml", cwd=self.clone)
        git(*base.ID, "commit", "-q", "-m", "evil", cwd=self.clone)
        self.run_publish()
        self.assertEqual((self.fake / "gitleaks.config").read_text().strip(), "# base config")

    def test_no_config_at_base_uses_defaults(self):
        self.run_publish()
        self.commit("two")
        (self.clone / ".gitleaks.toml").write_text("# allowlist\n")
        git("add", ".gitleaks.toml", cwd=self.clone)
        git(*base.ID, "commit", "-q", "-m", "evil", cwd=self.clone)
        self.run_publish()
        self.assertNotIn("--config", (self.fake / "gitleaks.log").read_text())


class TestReview(S2Case):
    def test_review_posted_scrubbed_then_ready(self):
        self.review_file()
        self.write_want(all_done=True)
        self.assertEqual(self.run_publish(), 0)
        got = self.result()
        self.assertEqual((got["state"], got["reviewed"], got["ready"]), ("published", True, True))
        body = self.reviews()[0]
        self.assertIn("Looks good", body)
        self.assertNotIn(BAD, body)
        self.assertNotIn(TOKEN, body)
        order = [c[:2] for c in self.calls()]
        self.assertLess(order.index(["pr", "comment"]), order.index(["pr", "review"]))
        self.assertLess(order.index(["pr", "review"]), order.index(["pr", "ready"]))
        review = self.names("pr", "review")[0]
        self.assertEqual(review[2:], ["7", "--comment", "--body-file", "-", "--repo", "o/r"])
        self.assertNotIn("--approve", review)

    def posted_body(self, text):
        self.review_file(text)
        self.write_want(all_done=True)
        self.assertEqual(self.run_publish(), 0)
        return self.reviews()[0]

    def test_review_redacts_aws_access_key(self):
        body = self.posted_body("ok AKIA" + "FAKEFAKEFAKE0000 and ASIA" + "FAKEFAKEFAKE1111 end\n")
        self.assertNotIn("FAKEFAKEFAKE", body)
        self.assertIn("ok ", body)

    def test_review_redacts_slack_token(self):
        body = self.posted_body("ok xox" + "b-0000000000-fakefakefake end\n")
        self.assertNotIn("fakefakefake", body)
        self.assertIn(" end", body)

    def test_review_redacts_private_key_block_and_bare_header(self):
        head = "-----BEGIN RSA PRIVATE " + "KEY-----"
        foot = "-----END RSA PRIVATE " + "KEY-----"
        body = self.posted_body(f"a {head}\nFAKEKEYBODY\n{foot} b\n{head} c\n")
        self.assertNotIn("FAKEKEYBODY", body)
        self.assertNotIn("PRIVATE KEY", body)
        self.assertIn(" b", body)

    def test_review_marker_survives_a_lost_result_flag(self):
        self.review_file()
        self.write_want(all_done=True)
        self.assertEqual(self.run_publish(), 0)
        got = self.result()
        self.assertIn(f"<!-- capsule-review:7:{got['head'][:12]}:{got['reviewed_sha256'][:12]} -->",
                      self.reviews()[0])
        (self.fake / "no_auth").write_text("")
        self.run_publish()
        self.assertEqual(self.result()["reason"], "gh_auth")
        (self.fake / "no_auth").unlink()
        self.assertEqual(self.run_publish(), 0)
        self.assertEqual(len(self.reviews()), 1)
        self.assertTrue(self.result()["reviewed"])

    def test_rerun_never_reposts(self):
        self.review_file()
        self.write_want(all_done=True)
        self.run_publish()
        self.run_publish()
        self.assertEqual(len(self.reviews()), 1)
        self.assertEqual(len(self.names("pr", "ready")), 1)
        self.assertTrue(self.result()["reviewed"] and self.result()["ready"])

    def test_not_done_or_no_file_means_slice_one(self):
        self.review_file()
        for over in ({}, {"all_done": False}):
            self.write_want(**over)
            self.run_publish()
        self.want_path.with_name("epic-x.review.md").unlink()
        self.write_want(all_done=True)
        self.run_publish()
        self.assertEqual(self.names("pr", "review") + self.names("pr", "ready"), [])
        self.assertNotIn("reviewed", self.result())

    def test_review_failure_keeps_earlier_work(self):
        self.review_file()
        self.write_want(all_done=True)
        (self.fake / "fail_review").write_text("")
        self.run_publish()
        got = self.result()
        self.assertEqual((got["reason"], got["detail"]), ("pr_failed", "gh pr review"))
        self.assertEqual(got["commented"], ["t_aaaa0001", "t_bbbb0002"])
        self.assertEqual((got["pr_number"], got["state"]), (7, "refused"))
        self.assertEqual(self.names("pr", "ready"), [])

    def test_ready_failure_then_rerun_only_retries_ready(self):
        self.review_file()
        self.write_want(all_done=True)
        (self.fake / "fail_ready").write_text("")
        self.run_publish()
        got = self.result()
        self.assertEqual((got["reason"], got["detail"], got["reviewed"]),
                         ("pr_failed", "gh pr ready", True))
        (self.fake / "fail_ready").unlink()
        self.run_publish()
        self.assertEqual(len(self.reviews()), 1)
        self.assertTrue(self.result()["ready"])

    def edits(self):
        return json.loads((self.fake / "edits.json").read_text())

    def ready_then_move_head(self, new_review=True):
        self.review_file()
        self.write_want(all_done=True)
        self.run_publish()
        first = self.result()
        (self.fake / "argv.log").unlink()
        self.commit("two")
        if new_review:
            self.review_file("A fresh review of the new head.\n")
        return first

    def test_moved_head_undoes_ready_and_reviews_again(self):
        first = self.ready_then_move_head()
        self.assertEqual(self.run_publish(), 0)
        got = self.result()
        self.assertNotEqual(got["head"], first["head"])
        undo = self.names("pr", "ready")
        self.assertEqual([c[2:] for c in undo],
                         [["--undo", "7", "--repo", "o/r"], ["7", "--repo", "o/r"]])
        self.assertEqual(len(self.reviews()), 2)
        self.assertIn(f"capsule-review:7:{got['head'][:12]}:{got['reviewed_sha256'][:12]}",
                      self.reviews()[1])
        self.assertEqual((got["reviewed_head"], got["ready"]), (got["head"], True))

    def test_moved_head_without_new_review_stays_draft_and_cleared(self):
        self.ready_then_move_head()
        self.want_path.with_name("epic-x.review.md").unlink()
        self.run_publish()
        got = self.result()
        self.assertEqual(len(self.names("pr", "ready")), 1)
        self.assertEqual(self.names("pr", "ready")[0][2], "--undo")
        for key in ("ready", "reviewed", "reviewed_head"):
            self.assertNotIn(key, got)

    def test_moved_head_with_same_review_file_stays_draft_and_stale(self):
        first = self.ready_then_move_head(new_review=False)
        self.run_publish()
        got = self.result()
        self.assertEqual(len(self.reviews()), 1)
        self.assertEqual([c[2] for c in self.names("pr", "ready")], ["--undo"])
        self.assertTrue(got["review_stale"])
        self.assertIn(f"unchanged since head {first['head'][:12]}", got["review_stale_reason"])
        self.assertIn(f"needed for {got['head'][:12]}", got["review_stale_reason"])
        self.assertNotIn("ready", got)

    def test_unchanged_head_makes_no_undo_or_second_review(self):
        self.ready_then_move_head()
        self.run_publish()
        (self.fake / "argv.log").unlink()
        self.run_publish()
        self.assertEqual(self.names("pr", "ready"), [])
        self.assertEqual(self.names("pr", "review"), [])
        self.assertEqual(len(self.reviews()), 2)

    def foreign_marker(self):
        """Result of one publish whose own marker was authored by someone else."""
        self.review_file()
        self.write_want(all_done=True)
        self.run_publish()
        (self.fake / "reviews.json").write_text(json.dumps(
            [{"body": self.reviews()[0], "login": "mallory"}]))
        return self.result()

    def test_marker_from_another_login_is_not_a_review(self):
        self.foreign_marker()
        self.run_publish()
        self.assertEqual(len(self.reviews()), 2)        # own review posted again
        self.assertEqual(self.reviews()[1].count("capsule-review:7:"), 1)

    def test_own_marker_counts(self):
        self.foreign_marker()
        (self.fake / "reviews.json").write_text(json.dumps([self.reviews()[0]["body"]]))
        self.run_publish()
        self.assertEqual(len(self.reviews()), 1)

    def test_poll_ignores_a_marker_from_another_login(self):
        doc = self.foreign_marker()
        view = {"number": 7, "state": "OPEN", "isDraft": False, "headRefOid": doc["head"],
                "reviews": [{"body": self.reviews()[0]["body"], "author": {"login": "mallory"}}]}
        (self.fake / "view.json").write_text(json.dumps(view))
        self.result_path.write_text(json.dumps({**doc, "polled_at": 0}))
        self.run_poll()
        self.assertTrue(self.result()["ready_unreviewed"])

    def test_unknown_login_refuses_and_keeps_the_stale_check(self):
        self.ready_then_move_head(new_review=False)
        (self.fake / "fail_api").write_text("")
        self.run_publish()
        got = self.result()
        self.assertEqual((got["state"], got["reason"]), ("refused", "gh_auth"))
        self.assertEqual(len(self.reviews()), 1)
        self.assertEqual([c[2] for c in self.names("pr", "ready")], ["--undo"])
        self.assertNotIn("ready", got)

    def test_blank_login_is_unknown(self):
        self.ready_then_move_head(new_review=False)
        (self.fake / "login.json").write_text('""')
        self.run_publish()
        self.assertEqual(self.result()["reason"], "gh_auth")
        self.assertEqual(len(self.reviews()), 1)

    def test_poll_with_unknown_login_is_an_error_not_unreviewed(self):
        doc = self.foreign_marker()
        view = {"number": 7, "state": "OPEN", "isDraft": False, "headRefOid": doc["head"],
                "reviews": [{"body": self.reviews()[0]["body"], "author": {"login": "me"}}]}
        (self.fake / "view.json").write_text(json.dumps(view))
        (self.fake / "fail_api").write_text("")
        self.result_path.write_text(json.dumps({**doc, "polled_at": 0}))
        self.run_poll()
        self.assertIn("poll_error", self.result())
        self.assertNotIn("ready_unreviewed", self.result())

    def test_missing_or_non_dict_author_is_ignored(self):
        body = "x\n<!-- capsule-review:7:" + "a" * 12 + " -->"
        for author in ({}, {"author": None}, {"author": "me"}, {"author": {"login": None}}):
            with mock.patch.object(publish, "_login", return_value="me"):
                self.assertEqual(publish._heads(7, [{"body": body, **author}]), {})

    def test_headless_marker_is_not_a_review_of_this_head(self):
        self.review_file()
        self.write_want(all_done=True)
        (self.fake / "reviews.json").write_text(json.dumps(["old\n<!-- capsule-review:7 -->\n"]))
        self.run_publish()
        self.assertEqual(len(self.reviews()), 2)

    def test_body_is_rewritten_with_current_cards_on_every_publish(self):
        self.run_publish()
        self.assertEqual(self.names("pr", "edit"), [])      # created with the body: no edit
        self.write_want(cards=[{**base.CARDS[0], "title": "Renamed card"}])
        self.run_publish()
        self.run_publish()
        self.assertEqual(len(self.edits()), 2)
        self.assertIn("t_aaaa0001: Renamed card", self.edits()[0])
        self.assertNotIn("t_bbbb0002", self.edits()[1])
        edit = self.names("pr", "edit")[0]
        self.assertEqual(edit[2:], ["7", "--body-file", "-", "--repo", "o/r"])

    def test_undo_failure_refuses_before_push_and_keeps_the_pr(self):
        self.ready_then_move_head()
        remote = self.remote_head()
        (self.fake / "fail_ready").write_text("")
        self.run_publish()
        got = self.result()
        self.assertEqual((got["reason"], got["ready_unreviewed"]), ("undo_failed", True))
        self.assertEqual(got["pr_number"], 7)
        self.assertEqual(self.remote_head(), remote)

    def test_refusal_then_moved_head_still_undoes(self):
        self.ready_then_move_head()
        (self.fake / "no_auth").write_text("")
        self.run_publish()
        (self.fake / "no_auth").unlink()
        self.run_publish()
        self.assertEqual(self.names("pr", "ready")[0][2], "--undo")

    def test_early_refusal_keeps_the_known_pr_and_poll_still_checks_it(self):
        self.run_publish()
        (self.fake / "no_repo").write_text("")
        self.run_publish()
        got = self.result()
        self.assertEqual((got["reason"], got["pr_number"]), ("repo_unknown", 7))
        self.assertIn("pr_url", got)
        (self.fake / "view.json").write_text(json.dumps(TestPoll.VIEW))
        (self.fake / "argv.log").unlink()
        self.run_poll()
        self.assertEqual([c[:3] for c in self.calls()], [["pr", "view", "7"]])

    def test_missing_is_draft_fails_closed(self):
        self.ready_then_move_head()
        (self.fake / "no_isdraft").write_text("")
        self.run_publish()
        self.assertEqual(self.names("pr", "ready")[0][2], "--undo")

    def test_edit_failure_keeps_pr_number(self):
        self.run_publish()
        (self.fake / "fail_edit").write_text("")
        self.run_publish()
        got = self.result()
        self.assertEqual((got["reason"], got["detail"], got["pr_number"]),
                         ("pr_failed", "gh pr edit", 7))

    def test_call_order_view_undo_push_edit_review_ready(self):
        self.ready_then_move_head()
        self.run_publish()
        order = [" ".join(c[:2]) for c in self.calls() if c[0] == "pr"]
        want = ["pr list", "pr view", "pr ready", "pr edit", "pr view", "pr review", "pr ready"]
        self.assertEqual([o for o in order if o != "pr view"],
                         [o for o in want if o != "pr view"])
        self.assertLess(self.calls().index(self.names("pr", "ready")[0]),
                        self.calls().index(self.names("pr", "edit")[0]))

    def test_never_merges_or_forces(self):
        self.review_file()
        self.write_want(all_done=True)
        self.run_publish()
        self.assertEqual(self.names("pr", "merge"), [])
        self.assertNotIn("--force", (self.fake / "git.log").read_text())


class TestPoll(S2Case):
    VIEW = {"number": 7, "state": "OPEN", "isDraft": True, "headRefOid": "abc"}

    def published(self):
        self.run_publish()
        self.want_before = self.want_path.read_text()
        (self.fake / "argv.log").unlink()
        (self.fake / "git.log").unlink()
        (self.fake / "view.json").write_text(json.dumps(self.VIEW))

    def stamp(self, **over):
        doc = {**self.result(), **over}
        self.result_path.write_text(json.dumps(doc))

    def test_poll_flags_a_ready_pr_with_no_review_of_its_head(self):
        self.published()
        (self.fake / "view.json").write_text(json.dumps({**self.VIEW, "isDraft": False}))
        self.run_poll()
        self.assertTrue(self.result()["ready_unreviewed"])
        (self.fake / "view.json").write_text(json.dumps(self.VIEW))
        self.stamp(polled_at=0)
        self.run_poll()
        self.assertNotIn("ready_unreviewed", self.result())

    def test_poll_stores_pr_and_time_without_other_calls(self):
        self.published()
        before = time.time()
        self.assertEqual(self.run_poll(), 0)
        got = self.result()
        self.assertEqual(got["pr"], self.VIEW)
        self.assertGreaterEqual(got["polled_at"], int(before))
        self.assertEqual(got["pr_number"], 7)
        calls = self.calls()
        self.assertEqual([c[:2] for c in calls], [["pr", "view"]])
        for field in ("mergeCommit", "statusCheckRollup", "reviewDecision", "headRefOid"):
            self.assertIn(field, calls[0][4])
        self.assertEqual(self.want_path.read_text(), self.want_before)
        self.assertFalse((self.fake / "git.log").exists())

    def test_recent_poll_is_skipped_old_one_runs(self):
        self.published()
        self.stamp(polled_at=int(time.time()) - 10)
        self.run_poll()
        self.assertEqual(self.calls(), [])
        self.stamp(polled_at=int(time.time()) - 301)
        self.run_poll()
        self.assertEqual(len(self.calls()), 1)

    def test_merged_pr_fetches_once_then_is_left_alone(self):
        self.published()
        git("push", "-q", str(self.remote), "main", cwd=self.clone)
        (self.fake / "git.log").unlink()
        (self.fake / "view.json").write_text(json.dumps({**self.VIEW, "state": "MERGED"}))
        self.run_poll()
        self.assertTrue(self.result()["fetched_after_merge"])
        fetch = (self.fake / "git.log").read_text().splitlines()
        self.assertEqual(len(fetch), 1)
        self.assertEqual(fetch[0], f"-C {self.clone} fetch -- {self.remote} main")
        self.stamp(polled_at=0)
        self.run_poll()
        self.assertEqual(len((self.fake / "git.log").read_text().splitlines()), 1)

    def test_gh_failure_keeps_fields_and_adds_error(self):
        self.published()
        (self.fake / "fail_view").write_text("")
        before = self.result()
        self.run_poll()
        got = self.result()
        self.assertEqual(got.pop("poll_error"), "gh pr view")
        self.assertEqual(got, before)

    def test_bad_json_and_fetch_failure_are_errors(self):
        self.published()
        (self.fake / "view.json").write_text("{nope")
        self.run_poll()
        self.assertEqual(self.result()["poll_error"], "gh pr view")
        (self.fake / "view.json").write_text(json.dumps({**self.VIEW, "state": "MERGED"}))
        self.want_path.write_text(self.want_before.replace(str(self.remote), "/no/such/repo"))
        self.run_poll()
        got = self.result()
        self.assertEqual(got["poll_error"], "git fetch")
        self.assertNotIn("pr", got)

    def test_one_bad_result_never_stops_the_others(self):
        self.published()
        pub = self.want_path.parent
        (pub / "aaa.result.json").write_text(json.dumps({"pr_number": 3}))   # no want file
        (pub / "bbb.result.json").write_text("not json")
        (pub / "ccc.result.json").write_text(json.dumps({"state": "refused"}))
        self.run_poll()
        self.assertEqual(json.loads((pub / "aaa.result.json").read_text())["poll_error"], "want")
        self.assertEqual(self.result()["pr"], self.VIEW)
        self.assertNotIn("poll_error", json.loads((pub / "ccc.result.json").read_text()))

    def test_switch_off_runs_nothing(self):
        self.published()
        self.write_config({"lookout": {"publish": False}})
        self.assertEqual(self.run_poll(), 0)
        self.assertEqual(self.calls(), [])
        self.assertNotIn("pr", self.result())


class LoginShapeTest(unittest.TestCase):
    def login(self, value):
        with mock.patch.object(publish.daily_pr, "_gh", return_value=(0, value + "\n")):
            return publish._login()

    def test_invalid_logins_are_unknown(self):
        for bad in ("-abc", "abc-", "a--b", "a" * 40, "", "null", "a b"):
            self.assertEqual(self.login(bad), "", bad)

    def test_valid_logins_are_kept(self):
        for good in ("a", "a-b", "a" * 39, "Owner-1"):
            self.assertEqual(self.login(good), good)


if __name__ == "__main__":
    unittest.main()
