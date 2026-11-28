"""R31 parts: the lint (`capsule/blind/lint.py`), the outcome classes (`red.classify`), the tester's
sparse clone and the blind run's use of the recorded commit. The end-to-end red check and landing
are in test_blind_red_tick.py. Temp repos only; no docker, no model.

Z2 oracles: for lint, the files on disk and the commit git built (`git ls-tree`, two data paths);
for the clone, `git ls-files`/`git log` in it and the directory listing."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from capsule import bundles, db, workspace
from capsule.blind import dispatch, launch, lint, red
from capsule.tests import tmpclean
from capsule.tests.test_blind_run import BOARD, TESTS, Container, RunCase, blind_repo, sh

CARD = "t_0a1b2c3d"
GOOD = ("import unittest\n\nfrom harness import run\n\n\nclass T(unittest.TestCase):\n"
        "    def test_a(self):\n        run('true')\n")
EXAMPLE = json.dumps({"id": f"{CARD}.test_a", "given": "g", "expect": "e",
                      "source": f"{CARD} acceptance 1"})


def header(card_hash="h" * 64):
    return json.dumps({"card": CARD, "card_hash": card_hash, "statements": [1]})


def exempting(card, ids):
    """A `make_round` mutate function: the tester's HEADER.json lists `ids` as `red_exempt`."""
    def mutate(clone):
        path = Path(clone) / "cards" / card / "HEADER.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), "red_exempt": ids}))
    return mutate


def write(root, rel, data):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    return path


class Dirs(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.root)

    def clone(self, extra=None):
        base = {f"cards/{CARD}/test_it.py": GOOD, f"cards/{CARD}/examples.jsonl": EXAMPLE + "\n",
                f"cards/{CARD}/HEADER.json": header()}
        for rel, data in {**base, **(extra or {})}.items():
            if data is not None:
                write(self.root / "c", rel, data)
        return self.root / "c"


class Collect(Dirs):
    def test_the_card_folder_is_collected_and_the_harness_copy_and_caches_are_not_output(self):
        write(self.root / "c", "harness/api.py", "x = 1\n")
        write(self.root / "c", ".git", "gitdir: ../x\n")
        write(self.root / "c", f"cards/{CARD}/__pycache__/a.cpython-314.pyc", b"\0")
        write(self.root / "c", f"cards/{CARD}/sub/helper.py", "y = 2\n")
        files, problems = lint.collect(self.clone(), CARD)
        names = ("HEADER.json", "examples.jsonl", "sub/helper.py", "test_it.py")
        want = sorted(f"cards/{CARD}/{n}" for n in names)
        self.assertEqual((sorted(files), problems), (want, []))

    def test_anything_outside_the_card_folder_is_a_problem(self):
        files, problems = lint.collect(self.clone({"cards/t_other/test_x.py": "", "README.md": "x",
                                                    "tests/test_y.py": ""}), CARD)
        self.assertEqual(problems, ["README.md: outside cards/t_0a1b2c3d/",
                                    "cards/t_other/test_x.py: outside cards/t_0a1b2c3d/",
                                    "tests/test_y.py: outside cards/t_0a1b2c3d/"])
        self.assertEqual(len(files), 3)

    def test_links_and_special_files_are_refused_and_never_followed(self):
        clone = self.clone()
        os.symlink("/etc/hostname", clone / "cards" / CARD / "link.txt")
        os.symlink("/etc", clone / "cards" / CARD / "dir")
        os.mkfifo(clone / "cards" / CARD / "pipe")
        files, problems = lint.collect(clone, CARD)
        self.assertEqual(sorted(problems), [
            f"cards/{CARD}/dir: not a regular file (a link or special file)",
            f"cards/{CARD}/link.txt: not a regular file (a link or special file)",
            f"cards/{CARD}/pipe: not a regular file (a link or special file)"])
        self.assertNotIn(f"cards/{CARD}/link.txt", files)

    def test_a_git_internals_name_is_refused(self):
        _, problems = lint.collect(self.clone({f"cards/{CARD}/.git/config": "x"}), CARD)
        self.assertEqual(len(problems), 1)
        self.assertIn("a file name git or a person cannot use", problems[0])

    def test_over_50_files_or_200_kb_is_refused_and_nothing_is_collected(self):
        many = {f"cards/{CARD}/f{n}.txt": "x" for n in range(lint.MAX_FILES)}   # + 3 base files
        files, problems = lint.collect(self.clone(many), CARD)
        self.assertEqual((files, len(problems)), ({}, 1))
        self.assertIn("over the caps: 50 files and 200 KB", problems[0])
        big = self.clone({f"cards/{CARD}/big.txt": "x" * (200 * 1024)})
        files, problems = lint.collect(big, CARD)
        self.assertEqual((files, len(problems)), ({}, 1))

    def test_exactly_50_files_pass(self):
        many = {f"cards/{CARD}/f{n}.txt": "x" for n in range(47)}
        files, problems = lint.collect(self.clone(many), CARD)
        self.assertEqual((len(files), problems), (50, []))


class Problems(unittest.TestCase):
    def files(self, **over):
        base = {f"cards/{CARD}/test_it.py": GOOD.encode(),
                f"cards/{CARD}/HEADER.json": header().encode(),
                f"cards/{CARD}/examples.jsonl": (EXAMPLE + "\n").encode()}
        for name, data in over.items():             # None removes the file
            base.pop(f"cards/{CARD}/{name}", None)
            if data is not None:
                base[f"cards/{CARD}/{name}"] = data.encode()
        return base

    def lint(self, **over):
        return lint.problems(self.files(**over), CARD, "h" * 64)

    def test_clean_files_pass(self):
        self.assertEqual(self.lint(), [])

    def test_a_header_with_another_hash_or_none_is_a_problem(self):
        stale = self.lint(**{"HEADER.json": header("a" * 64)})
        self.assertIn("is not the card's current hash", stale[0])
        self.assertEqual(self.lint(**{"HEADER.json": None}), ["HEADER.json is missing"])
        self.assertEqual(self.lint(**{"HEADER.json": "{"}), ["HEADER.json is not JSON"])

    def test_an_example_row_needs_a_source(self):
        row = json.dumps({"id": f"{CARD}.test_a", "given": "g", "expect": "e", "source": "  "})
        self.assertEqual(self.lint(**{"examples.jsonl": row}),
                         [f"example row {CARD}.test_a has an empty source"])
        self.assertEqual(self.lint(**{"examples.jsonl": None}), ["examples.jsonl is missing"])
        self.assertEqual(self.lint(**{"examples.jsonl": "nope"}),
                         ["examples.jsonl line 1 is not JSON"])

    def test_only_the_stdlib_the_harness_and_the_own_folder_may_be_imported(self):
        for bad in ("import capsule.tick", "from capsule import tick", "import requests",
                    "__import__('capsule')", "import importlib"):
            self.assertEqual(len(self.lint(**{"test_it.py": GOOD + bad + "\n"})), 1, bad)
        ok = GOOD + "import os, json\nfrom harness import read\nimport helper\nfrom . import x\n"
        self.assertEqual(self.lint(**{"test_it.py": ok, "helper.py": "z = 1\n"}), [])

    def test_a_file_that_does_not_compile_is_named(self):
        self.assertEqual(self.lint(**{"test_it.py": "def (:\n"}),
                         [f"cards/{CARD}/test_it.py: does not compile"])

    def test_a_folder_without_a_test_file_is_a_problem(self):
        self.assertEqual(self.lint(**{"test_it.py": None}),
                         [f"cards/{CARD}/ holds no test_*.py file"])

    def test_the_test_ids_that_have_no_example_row_are_named(self):
        files = self.files()
        self.assertEqual(red._missing_examples(files, CARD, [f"{CARD}.test_a", f"{CARD}.test_b"]),
                         [f"{CARD}.test_b"])


class Stage(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.root)
        self.repo = blind_repo(self.root, CARD, TESTS)
        self.main = sh("git", "-C", str(self.repo), "rev-parse", "main").strip()

    def tree(self, commit):
        return sh("git", "-C", str(self.repo), "ls-tree", "-r", "--name-only", commit).split()

    def test_the_commit_holds_main_plus_exactly_the_card_folder_and_main_does_not_move(self):
        commit = red._stage(self.repo, CARD, {f"cards/{CARD}/test_new.py": b"x = 1\n"}, "tests")
        parse = ("git", "-C", str(self.repo), "rev-parse")
        self.assertEqual(sh(*parse, f"{commit}^").strip(), self.main)
        self.assertEqual(sh(*parse, f"card/{CARD}").strip(), commit)
        self.assertEqual(sh("git", "-C", str(self.repo), "rev-parse", "main").strip(), self.main)
        self.assertEqual([f for f in self.tree(commit) if f.startswith("cards/t_")],
                         [f"cards/{CARD}/test_new.py"])      # the folder's old files are gone
        self.assertIn("README.md", self.tree(commit))
        status = sh("git", "-C", str(self.repo), "status", "--porcelain")
        self.assertEqual(status, "")                                       # no checkout

    def test_the_bytes_are_kept_exactly(self):
        data = b"\xff\xfe binary \r\n\x00 tail"
        commit = red._stage(self.repo, CARD, {f"cards/{CARD}/x.bin": data}, "tests")
        shown = subprocess.run(["git", "-C", str(self.repo), "show",
                                f"{commit}:cards/{CARD}/x.bin"],
                               capture_output=True).stdout
        self.assertEqual(shown, data)

    def test_it_refuses_when_main_is_not_checked_out(self):
        sh("git", "-C", str(self.repo), "checkout", "-q", "-b", "other")
        with self.assertRaises(RuntimeError) as got:
            red._stage(self.repo, CARD, {f"cards/{CARD}/a.py": b""}, "tests")
        self.assertIn("other checked out, not main", str(got.exception))


class Classify(unittest.TestCase):
    def runs(self, *rows):
        return [{"t.a": row} for row in rows]

    def test_every_class(self):
        ran = {"cmd": "x", "exit": 1}
        fail, ok = {"result": "fail", "observed": ran}, {"result": "pass"}
        seen = {"result": "error", "observed": ran}
        early = {"result": "error"}
        for rows, want in (([fail] * 5, "ok"), ([seen] * 5, "inconclusive"),
                           ([ok] * 5, "passes-on-base"),
                           ([{"result": "fail"}] * 5, "errors-before-first-call"),
                           ([early] * 5, "errors-before-first-call"),
                           ([fail] * 3 + [ok] * 2, "inconclusive"),
                           ([fail] * 4 + [early], "inconclusive"),
                           ([{"result": "skipped"}] * 5, "inconclusive"),
                           ([fail] * 4 + [None], "inconclusive")):
            self.assertEqual(red.classify(self.runs(*rows)), {"t.a": want}, rows)

    def test_a_pass_on_base_is_ok_only_for_an_exempt_name(self):
        runs = [{"c.test_a": {"result": "pass"}, "c.test_b": {"result": "pass"}}] * 5
        self.assertEqual(red.classify(runs, {"c.test_a"}),
                         {"c.test_a": "ok", "c.test_b": "passes-on-base"})
        self.assertEqual(red.classify(runs, {"test_a"})["c.test_a"], "passes-on-base")

    def test_no_runs_no_outcomes(self):
        self.assertEqual(red.classify([]), {})


class SparseClone(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.root)
        self.repo = blind_repo(self.root, CARD, TESTS)
        other = self.repo / "cards" / "t_ffffffff"
        other.mkdir()
        (other / "test_secret.py").write_text("SECRET = 1\n")
        sh("git", "-C", str(self.repo), "add", "-A")
        sh("git", "-C", str(self.repo), *workspace.IDENTITY, "commit", "-q", "-m", "another card")
        self.head = sh("git", "-C", str(self.repo), "rev-parse", "HEAD").strip()

    def listing(self, clone):
        return sorted(p.relative_to(clone).as_posix() for p in clone.rglob("*")
                      if p.is_file() and "__pycache__" not in p.parts and p.name != ".git")

    def test_a_first_run_holds_only_the_harness_and_an_empty_card_folder(self):
        clone = launch._blind_clone(self.repo, self.root / "w" / "a", CARD)
        self.assertEqual([f for f in self.listing(clone) if not f.startswith("harness/")], [])
        self.assertTrue((clone / "cards" / CARD).is_dir())
        self.assertTrue((clone / "harness" / "api.py").is_file())
        gd = workspace.gitdir(clone)
        log = sh("git", f"--git-dir={gd}", "log", "--format=%s").split("\n")
        self.assertEqual([x for x in log if x], ["tests so far"])           # one commit, no history
        tree = sh("git", f"--git-dir={gd}", "ls-tree", "-r", "--name-only", "HEAD")
        self.assertNotIn("t_ffffffff", tree)
        branch = sh("git", f"--git-dir={gd}", "branch", "--show-current").strip()
        self.assertEqual(branch, f"card/{CARD}")

    def test_a_rerun_gets_its_own_cards_files_from_the_seed_and_no_other_card(self):
        clone = launch._blind_clone(self.repo, self.root / "w" / "b", CARD, self.head)
        files = self.listing(clone)
        self.assertEqual([f for f in files if f.startswith("cards/")],
                         [f"cards/{CARD}/examples.jsonl", f"cards/{CARD}/test_it.py"])
        self.assertEqual((clone / "cards" / CARD / "test_it.py").read_text(), TESTS)
        self.assertFalse((clone / "README.md").exists())
        self.assertFalse((clone / "blind.toml").exists())


class RecordedCommit(RunCase):
    def test_the_run_uses_the_commit_recorded_on_the_card_not_the_blind_head(self):
        recorded = sh("git", "-C", str(self.repo), "rev-parse", "HEAD").strip()
        (self.repo / "cards" / self.card / "test_it.py").write_text(
            TESTS.replace("test_greets", "test_renamed"))
        sh("git", "-C", str(self.repo), "add", "-A")
        sh("git", "-C", str(self.repo), *workspace.IDENTITY, "commit", "-q", "-m", "later")
        rows = self.go(Container(self.ws))
        self.assertEqual(sorted(r["cmd"] for r in rows),
                         [f"blind:{self.card}.test_fails", f"blind:{self.card}.test_greets"])
        self.assertEqual(self.result()["blind_commit"], recorded)

    def test_a_card_whose_tests_never_landed_gets_no_blind_run(self):
        self.conn.execute("UPDATE tasks SET blind_commit=NULL WHERE id=?", (self.card,))
        box = Container(self.ws)
        self.assertEqual((self.go(box), box.started), ([], []))

    def test_only_a_full_commit_id_is_used(self):
        for junk in ("HEAD", "--all", "abc", "Z" * 40):
            self.conn.execute("UPDATE tasks SET blind_commit=? WHERE id=?", (junk, self.card))
            self.assertEqual(self.go(Container(self.ws)), [], junk)


class StaleDispatch(RunCase):
    """A body change after landing starts no tester run when the acceptance text is the same."""

    def test_an_unchanged_acceptance_text_is_skipped_once_and_final(self):
        body = "Acceptance:\n- one\n- two\n"
        self.conn.execute("UPDATE tasks SET body=? WHERE id=?", (body, self.card))
        db.event(self.conn, self.card, "blind_landed", {
            "blind_commit": "a" * 40, "card_hash": bundles.card_hash("old"), "start_sha": "b" * 40,
            "tests": [], "acceptance": ["one", "two"], "exempt": []}, now=1)
        task = self.conn.execute("SELECT * FROM tasks WHERE id=?", (self.card,)).fetchone()
        for _ in range(2):
            dispatch._dispatch_one(object(), self.conn, self.cfg(), task, 5, {})
        skips = [json.loads(r[0]) for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='blind_tester_skipped'")]
        why = "the acceptance text is unchanged since the tests landed"
        self.assertEqual(skips, [{"reason": why, "card_hash": bundles.card_hash(body),
                                  "final": True}])
        self.assertTrue(dispatch._known(self.conn, self.card, bundles.card_hash(body)))
        stale = [json.loads(r[0]) for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='blind_tests_stale'")]
        self.assertEqual([s["acceptance_changed"] for s in stale], [False])


if __name__ == "__main__":
    unittest.main()
