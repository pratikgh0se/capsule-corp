"""C07a (t_2216591d): the lander's fetch, deploy and docker-hold checks, on the same real-git
world as test_lander.py. What the sandbox sets and inherits: test_sandbox.py."""
import json
import re
import subprocess
import unittest
from pathlib import Path

from capsule import db, lander

from capsule.tests.test_lander import World, git


class Deploy(World):
    def test_a_failing_deploy_check_is_reported_but_never_red(self):
        self.put("a.txt", "work\n", "card work")
        self.cfg["lander"]["deploy_check"] = "echo drift-seen; exit 1"
        result = self.check()
        self.assertEqual(result["verdict"], "checked")
        self.assertEqual(self.status(result)["deploy"], "advisory")
        self.assertIn("drift-seen", self.detail(result, "deploy"))


class Fetch(World):
    def test_an_unreachable_origin_waits_instead_of_checking_against_a_stale_main(self):
        self.put("a.txt", "work\n", "card work")
        git(self.repo, "remote", "set-url", "origin", str(self.dir / "gone.git"))
        result = self.check()
        self.assertEqual(result["verdict"], "wait")
        self.assertEqual(result["hold"], "lander_wait_fetch")
        self.assertEqual(self.status(result)["suites"], "skipped")

    def test_a_new_main_on_the_remote_is_seen_through_the_fetch(self):
        self.put("a.txt", "work\n", "card work")
        other = self.dir / "other"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(other)], check=True,
                       capture_output=True)
        for name in ("n.txt", "a.txt"):          # a clean move replays; one on a.txt conflicts
            (other / name).write_text("new\n")
            git(other, "add", "-A")
            git(other, "commit", "-qm", "main moved elsewhere")
            git(other, "push", "-q", "origin", "main")
            if name == "n.txt":
                self.assertEqual(self.status(self.check(fetch=False))["sync"], "pass")
                self.assertIn("replayed", self.detail(self.check(), "sync"))
        self.assertEqual(self.status(self.check())["sync"], "wait")


class Docker(World):
    """The sandbox has no docker socket: a suite that needs docker is held for the keeper."""

    def setUp(self):
        super().setUp()
        self.put("a.txt", "work\n", "card work")
        self.cfg["contract_commands"] = ["echo plain", "echo needs-docker"]
        self.cfg["lander"]["needs_docker"] = ["echo needs-docker"]

    def test_a_listed_docker_suite_holds_the_card_and_runs_no_suite(self):
        result = self.check()
        self.assertEqual(result["verdict"], "held")
        self.assertEqual(result["hold"], "needs_docker_tests")
        self.assertEqual(self.status(result)["suites"], "hold")
        self.assertIn("needs docker tests: land by keeper", self.detail(result, "suites"))

    def test_no_docker_suite_listed_runs_every_suite(self):
        del self.cfg["lander"]["needs_docker"]
        result = self.check()
        self.assertEqual(result["verdict"], "checked")
        self.assertIn("needs-docker", self.detail(result, "suites"))


class DockerPathDiff(World):
    """rq_5c41d53d: a diff touching a docker path needs docker even when no command text says so."""

    def setUp(self):
        super().setUp()
        self.cfg["contract_commands"] = ["echo plain"]

    def test_a_docker_path_diff_is_held_for_the_keeper_and_counts_no_red(self):
        self.put("docker/Dockerfile", "x\n", "card work")
        result = self.check()
        self.assertEqual(result["verdict"], "held")
        self.assertEqual(result["hold"], "needs_docker_tests")
        self.assertEqual(self.status(result)["suites"], "hold")
        self.assertNotIn("plain", self.detail(result, "suites"))

    def test_a_plain_diff_runs(self):
        self.put("a.txt", "work\n", "card work")
        self.assertEqual(self.check()["verdict"], "checked")

    def test_a_failed_diff_counts_as_no_docker_diff(self):
        self.assertFalse(lander._docker_diff(self.repo, "nosuchbase", "nosuchhead"))

    def test_a_docker_path_in_an_earlier_commit_still_holds_the_card(self):
        self.put("docker/Dockerfile", "x\n", "first commit")
        self.put("docs/note.md", "words\n", "second commit, docs only")
        self.assertEqual(self.check()["verdict"], "held")

    def test_a_rename_out_of_a_docker_path_still_holds_the_card(self):
        """Hunt #3: without --no-renames git lists only the new name, so a card that moves a
        docker file to a plain path would pass as checked."""
        for n, old in enumerate(("scripts/backup_pg.sh", "observability/sub/x.yaml",
                                 "maintenance/migrations/0001.sql")):
            with self.subTest(old=old):
                git(self.repo, "checkout", "-q", "main")
                self.put(old, "x\n", f"main has {old}")
                git(self.repo, "push", "-q", "origin", "main")
                branch = f"epic/rename{n}"
                git(self.repo, "checkout", "-q", "-b", branch)
                (self.repo / "plain").mkdir(exist_ok=True)
                git(self.repo, "mv", old, f"plain/moved{n}.txt")
                git(self.repo, "commit", "-qm", "card only moves the file out")
                result = lander.run(self.repo, branch, cfg=self.cfg)
                self.assertEqual(result["verdict"], "held")
                self.assertEqual(result["hold"], "needs_docker_tests")

    def test_the_docker_path_pattern_matches_exactly_the_docker_files(self):
        """Z2: a table, so narrowing any alternative of the pattern is caught."""
        for path in ("docker/Dockerfile", "observability/x.json", "observability/sub/x.yaml",
                     "maintenance/a.sql", "maintenance/migrations/0001.sql",
                     "scripts/backup_pg.sh", "x_pg.sh", "tests/lib_docker_optin.sh",
                     "tests/test_docker_image.sh", "tests/test_fluent_bit_scrub.sh"):
            self.assertTrue(re.match(lander.DOCKER_PATHS, path), path)
        for path in ("maintenance/notes.md", "maintenance/notes.txt", "src/docker.py",
                     "tests/test_docker_image.sh.bak"):
            self.assertFalse(re.match(lander.DOCKER_PATHS, path), path)

    def test_the_python_path_set_equals_the_shell_runners(self):
        """Z2: run_shell_suites.sh is the second oracle for the mirrored pattern."""
        sh = Path(__file__).resolve().parents[2] / "tests/run_shell_suites.sh"
        out = subprocess.run(["bash", "-c", f"eval \"$(grep '^DOCKER_PATHS=' {sh})\"; "
                              'printf %s "$DOCKER_PATHS"'], capture_output=True, text=True,
                             check=True).stdout
        self.assertEqual(lander.DOCKER_PATHS, out)


if __name__ == "__main__":
    unittest.main()


class FindingsPaths(World):
    """W9-4: a check's detail names the clone root `<clone>/`, never the throwaway temp dir."""

    def test_the_clone_root_the_check_ran_in_reads_clone(self):
        self.put("a.txt", "work\n", "card work")
        self.cfg["lander"]["gates"] = ["echo ran in $(pwd -P)/capsule/tick.py:12; exit 1"]
        detail = self.detail(self.check(), "gates")
        self.assertIn("ran in <clone>/capsule/tick.py:12", detail)
        self.assertNotIn("lander-", detail)

    def test_a_detail_without_a_clone_path_is_unchanged(self):
        self.put("a.txt", "work\n", "card work")
        self.cfg["lander"]["gates"] = ["echo plain /usr/lib/x.py:3 ok"]
        self.assertEqual(self.detail(self.check(), "gates"),
                         "$ echo plain /usr/lib/x.py:3 ok\nplain /usr/lib/x.py:3 ok")

    def test_the_cap_applies_after_the_rewrite(self):
        self.put("a.txt", "work\n", "card work")
        self.cfg["lander"]["gates"] = ["for i in $(seq 300); do echo $(pwd -P)/x$i; done"]
        detail = self.detail(self.check(), "gates")
        self.assertLessEqual(len(detail), lander.DETAIL)
        self.assertIn("<clone>/x300", detail)


    def test_a_stored_event_names_the_clone_root_as_clone(self):
        board = self.dir / "board.db"
        db.create_board(board)
        conn = db.connect(board)
        self.addCleanup(conn.close)
        tid = db.new_task(conn, title="t", assignee="dev", branch="b", status="done")
        path, plain = "/private/tmp/lander-ab12/c/capsule/tick.py:12", "/usr/lib/x.py:3"
        result = {"verdict": "red", "hold": "", "commit": None, "base": "b", "checks": [
            {"name": "blind", "status": "fail", "detail": f"E {path} {plain}"}]}
        lander.record(conn, tid, result, limit=3)
        row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND"
                           " kind='landing_checked'", (tid,)).fetchone()
        self.assertEqual(json.loads(row[0])["checks"][0]["detail"],
                         f"E <clone>/capsule/tick.py:12 {plain}")
