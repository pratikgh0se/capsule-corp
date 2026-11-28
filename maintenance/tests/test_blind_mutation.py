"""R36: the weekly blind-suite mutation score (`score`). Fixture repos live in temp dirs and `run`
is a fake; nothing here starts docker."""
import itertools
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from blind_mutation_fixture import (BROKEN, CALC, FIVE, ONE, PROJECT, RED, TODAY, TWO,  # noqa: E402
                                    Base, bm, fake_run, git, is_pristine, make_repo)


class ScoreTests(Base):
    def test_kills_and_survivors_are_counted_per_card_and_in_totals(self):
        doc = self.score()
        one, two = doc["cards"][ONE], doc["cards"][TWO]
        self.assertEqual((one["tried"], one["killed"], one["survived"], one["inconclusive"]),
                         (6, 2, 4, 0))
        self.assertEqual(one["score"], 0.3333)
        self.assertEqual((two["killed"], two["survived"], two["score"]), (6, 0, 1.0))
        self.assertEqual(doc["totals"], {"tried": 6, "killed": 6, "survived": 0, "inconclusive": 0,
                                         "score": 1.0, "survivors": []})
        self.assertIs(doc["truncated"], False)

    def test_survivors_are_exactly_file_line_mutator_at_the_commit(self):
        survivors = self.score()["cards"][ONE]["survivors"]
        self.assertEqual(sorted(map(tuple, (s.items() for s in survivors))), sorted([
            (("file", "calc.py"), ("line", 6), ("mutator", "comparison_swap")),
            (("file", "calc.py"), ("line", 6), ("mutator", "return_none")),
            (("file", "calc.py"), ("line", 10), ("mutator", "boolean_flip")),
            (("file", "calc.py"), ("line", 10), ("mutator", "return_none"))]))

    def test_the_report_file_has_the_documented_keys_and_is_replaced_on_a_second_run(self):
        doc = self.score()
        path = self.out / f"blind-mutation-{TODAY}.json"
        on_disk = json.loads(path.read_text())
        self.assertEqual(on_disk, doc)
        self.assertEqual(set(on_disk), {"schema", "date", "project_commit", "blind_commit",
                                        "truncated", "cards", "totals"})
        self.assertEqual((on_disk["schema"], on_disk["date"]), ("blind_mutation.v1", TODAY))
        self.assertEqual(on_disk["project_commit"], git(self.project, "rev-parse", "HEAD"))
        self.assertEqual(on_disk["blind_commit"], git(self.blind, "rev-parse", "HEAD"))
        self.score(max_mutants=1)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), [path.name])
        self.assertEqual(json.loads(path.read_text())["totals"]["tried"], 1)

    def test_a_none_from_run_is_inconclusive_never_a_kill(self):
        doc = self.score(lambda tree, card_dir: True if is_pristine(tree) else None)
        row = doc["cards"][ONE]
        self.assertEqual((row["killed"], row["survived"], row["inconclusive"]), (0, 0, 6))
        self.assertIsNone(row["score"])
        self.assertEqual((doc["totals"]["inconclusive"], doc["totals"]["score"]), (6, None))

    def test_a_red_or_broken_baseline_is_reported_with_no_mutants_and_others_still_score(self):
        self.add_cards(RED, BROKEN)
        cards = self.score()["cards"]
        for card, why in ((RED, "red"), (BROKEN, "error")):
            self.assertEqual(cards[card], {"tried": 0, "killed": 0, "survived": 0,
                                           "inconclusive": 0, "score": None, "survivors": [],
                                           "baseline": why})
        self.assertEqual(cards[TWO]["score"], 1.0)

    def test_no_cards_gives_empty_cards_and_a_null_total(self):
        self.blind = make_repo(self.tmp / "empty-blind", {"blind.toml": "version = 1\n"})
        doc = self.score()
        self.assertEqual((doc["cards"], doc["totals"]["score"], doc["truncated"]),
                         ({}, None, False))
        self.assertTrue((self.out / f"blind-mutation-{TODAY}.json").is_file())

    def test_only_tracked_non_test_valid_utf8_python_is_mutated(self):
        seen, first = set(), set()

        def run(tree, card_dir):
            if card_dir.name not in first:
                first.add(card_dir.name)
                return True
            for rel, body in PROJECT.items():
                want = body if isinstance(body, bytes) else body.encode()
                if rel.endswith(".py") and (tree / rel).read_bytes() != want:
                    seen.add(rel)
            return False
        (self.project / "untracked.py").write_text("def u():\n    return True\n")
        self.score(run)
        self.assertEqual(seen, {"calc.py"})

    def test_the_report_holds_locations_only(self):
        self.score()
        text = (self.out / f"blind-mutation-{TODAY}.json").read_text()
        for leaked in ("SECRET TEST SOURCE", "test_it", "def add", "return a + b", "blind.toml"):
            self.assertNotIn(leaked, text)

    def test_the_sampler_is_deterministic_per_seed_and_capped(self):
        a, b = self.score(max_mutants=3, seed=7), self.score(max_mutants=3, seed=7)
        self.assertEqual(a, b)
        self.assertEqual({c["tried"] for c in a["cards"].values()}, {3})
        picks = {json.dumps(self.score(max_mutants=2, seed=s)["cards"][ONE]) for s in range(8)}
        self.assertGreater(len(picks), 1)
        self.assertEqual(self.score(max_mutants=0)["totals"]["tried"], 0)


class SharedSampleTests(Base):
    def test_every_card_runs_the_same_mutants(self):
        ran = {}

        def run(tree, card_dir):
            if not is_pristine(tree):
                ran.setdefault(card_dir.name, []).append((tree / "calc.py").read_text())
            return True
        self.score(run, max_mutants=4, seed=3)
        self.assertEqual(ran[ONE], ran[TWO])
        self.assertEqual(len(ran[ONE]), 4)

    def test_totals_count_each_mutant_once_and_list_the_ones_no_card_killed(self):
        self.add_cards(FIVE)
        doc = self.score()
        self.assertEqual(doc["cards"][ONE]["tried"] + doc["cards"][FIVE]["tried"], 12)
        self.assertEqual((doc["totals"]["tried"], doc["totals"]["killed"]), (6, 6))
        self.assertEqual(doc["totals"]["survivors"], [])        # TWO kills what ONE and FIVE miss

        def run(tree, card_dir):
            return None if card_dir.name == TWO else fake_run(tree, card_dir)
        totals = self.score(run)["totals"]
        self.assertEqual((totals["tried"], totals["killed"], totals["survived"]), (6, 2, 4))
        self.assertEqual(totals["score"], 0.3333)
        self.assertEqual(len(totals["survivors"]), 4)

    def test_a_mutant_is_killed_if_any_card_kills_survived_if_none_do_else_inconclusive(self):
        def by_card(verdicts):
            return lambda tree, card_dir: (True if is_pristine(tree) else verdicts[card_dir.name])
        cases = [({ONE: False, TWO: True}, (6, 0, 0)), ({ONE: True, TWO: None}, (0, 6, 0)),
                 ({ONE: None, TWO: None}, (0, 0, 6))]
        for verdicts, (killed, survived, inconclusive) in cases:
            totals = self.score(by_card(verdicts))["totals"]
            self.assertEqual((totals["killed"], totals["survived"], totals["inconclusive"]),
                             (killed, survived, inconclusive), verdicts)

    def test_the_deadline_stops_new_mutants_and_marks_the_report_truncated(self):
        doc = self.score(deadline_seconds=0)
        self.assertEqual((doc["truncated"], doc["totals"]["tried"]), (True, 0))
        self.assertEqual(doc["cards"][ONE]["tried"], 0)
        on_disk = json.loads((self.out / f"blind-mutation-{TODAY}.json").read_text())
        self.assertIs(on_disk["truncated"], True)
        with mock.patch.object(bm.time, "monotonic", side_effect=itertools.count()):
            doc = self.score(deadline_seconds=3)
        self.assertEqual((doc["truncated"], doc["totals"]["tried"]), (True, 2))
        self.assertEqual(doc["cards"][TWO]["tried"], 2)


    def test_a_truncated_run_does_not_always_score_the_same_early_mutants(self):
        firsts = set()
        for seed in range(8):
            seen = []

            def run(tree, card_dir):
                if not is_pristine(tree) and not seen:
                    seen.append((tree / "calc.py").read_text())
                return True
            with mock.patch.object(bm.time, "monotonic", side_effect=itertools.count()):
                self.score(run, deadline_seconds=2, seed=seed)
            firsts.add(seen[0])
        self.assertGreater(len(firsts), 1)


class OddFilesTests(Base):
    """Files that cannot be mutated are skipped; they never stop the job."""

    def commit_odd_files(self):
        os.symlink("/etc/hosts", self.project / "link.py")            # an absolute symlink
        (self.project / ".gitattributes").write_text("ignored.py export-ignore\n")
        (self.project / "ignored.py").write_text("def g():\n    return True\n")
        git(self.project, "add", "-A")
        blob = subprocess.run(["git", "-C", str(self.project), "hash-object", "-w", "--stdin"],
                              input=b"def f():\n    return True\n", capture_output=True,
                              check=True).stdout.strip()
        subprocess.run([b"git", b"-C", os.fsencode(self.project), b"update-index", b"--add",
                        b"--cacheinfo", b"100644," + blob + b",caf\xe9.py"],   # non-UTF-8 name
                       check=True, capture_output=True)
        git(self.project, "commit", "-q", "-m", "odd files")

    def test_an_absolute_symlink_an_export_ignored_file_and_a_non_utf8_name_are_skipped(self):
        self.commit_odd_files()
        doc = self.score()
        self.assertEqual((doc["totals"]["tried"], doc["cards"][TWO]["score"]), (6, 1.0))
        self.assertTrue((self.out / f"blind-mutation-{TODAY}.json").is_file())

    def test_a_git_timeout_or_undecodable_output_is_a_runtime_error_not_a_traceback(self):
        bad_output = UnicodeDecodeError("utf-8", b"", 0, 1, "x")
        for exc in (subprocess.TimeoutExpired("git", 60), bad_output):
            with mock.patch.object(bm.preflight, "git", side_effect=exc):
                with self.assertRaises(RuntimeError):
                    self.score()


class SafetyTests(Base):
    def snapshot(self, repo):
        listing = sorted(str(p.relative_to(repo)) for p in repo.rglob("*") if ".git" not in p.parts)
        return (git(repo, "rev-parse", "HEAD"), git(repo, "for-each-ref"),
                git(repo, "status", "--porcelain"), (repo / ".git" / "index").read_bytes(), listing)

    def test_project_and_blind_repo_are_untouched(self):
        before = self.snapshot(self.project), self.snapshot(self.blind)
        self.score()
        self.assertEqual((self.snapshot(self.project), self.snapshot(self.blind)), before)

    def test_every_mutant_gets_a_fresh_tree_and_runs_are_one_at_a_time(self):
        live, dirty = [], []

        def run(tree, card_dir):
            live.append(1)
            self.assertEqual(len(live), 1)
            dirty.append((tree / "left-behind").exists())
            (tree / "left-behind").write_text("x")
            (tree / "calc.py").write_text(CALC)
            live.pop()
            return True
        self.score(run)
        self.assertGreater(len(dirty), 10)
        self.assertFalse(any(dirty))

    def test_temp_copies_go_away_and_a_raising_run_leaves_no_report(self):
        sandbox = self.tmp / "sandbox"
        sandbox.mkdir()
        with mock.patch.object(tempfile, "tempdir", str(sandbox)):
            self.score()
            self.assertEqual(list(sandbox.iterdir()), [])
            calls = []

            def boom(tree, card_dir):
                calls.append(1)
                if len(calls) > 3:
                    raise RuntimeError("docker fell over")
                return True
            for p in self.out.iterdir():
                p.unlink()
            with self.assertRaises(RuntimeError):
                self.score(boom)
            self.assertEqual(list(sandbox.iterdir()), [])
        self.assertEqual(list(self.out.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
