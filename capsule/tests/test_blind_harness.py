"""R13: the blind harness API and its unittest runner (blind spec 3.3, 4.2). The runner is exercised
the way the tick will use it: the package copied to a scratch dir as `harness`, started with the
`run` line `capsule blind init` writes into blind.toml, from a workspace the product controls; its
two outputs (results.jsonl, summary line) are read back."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from capsule import paths
from capsule.blind import cli, harness
from capsule.blind.harness import api

SECRET = "SECRET-EXPECTED-VALUE-42"
INTENT_FILE = paths.INTENT / "blind_harness.jsonl"
INTENT_IDS = {"product-planted-harness-changes-nothing",
              "subtest-and-setup-errors-reconcile-with-summary"}


class RunnerTest(unittest.TestCase):
    """The harness copied to <tmp>/blind/harness, a card to <tmp>/blind/cards/<id>, a product dir.
    """
    card = "t_0000aaaa"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.blind = self.tmp / "blind"
        shutil.copytree(Path(harness.__file__).parent, self.blind / "harness",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.cards = self.blind / "cards" / self.card
        self.cards.mkdir(parents=True)
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        (self.ws / "tool.sh").write_text("#!/bin/sh\necho version 1\n")
        self.out = self.tmp / "out"

    def write(self, name, body, where=None):
        path = (where or self.cards) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body))

    def runner(self, *extra):
        """Run the exact line blind.toml carries (paths remapped), starting in the workspace."""
        line = cli.RUN_LINES["unittest"][0].format(card=self.card)
        line = line.replace("/blind", str(self.blind)) + f" --out {self.out} " + " ".join(extra)
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env[api.WORKSPACE_ENV] = str(self.ws)
        proc = subprocess.run(line, shell=True, cwd=self.ws, capture_output=True, text=True,
                              env=env)
        rows = []
        if (self.out / "results.jsonl").exists():
            rows = [json.loads(x) for x in (self.out / "results.jsonl").read_text().splitlines()]
        return proc, rows

    def contract(self, proc, rows, holders=0):
        """The comparison contract in the runner's docstring; returns (ran, status line)."""
        ran = int(re.search(r"Ran (\d+) tests?", proc.stdout).group(1))
        status = proc.stdout.strip().splitlines()[-1]
        self.assertEqual(len(rows), ran + holders)
        bad = any(r["result"] in ("fail", "error") for r in rows)
        self.assertEqual((bad, bad), (status.startswith("FAILED"), proc.returncode != 0))
        skipped = re.search(r"skipped=(\d+)", status)
        want = int(skipped.group(1)) if skipped else 0
        self.assertLessEqual(sum(r["result"] == "skipped" for r in rows), want)
        return ran, status


FAILING = """
    import unittest
    import harness

    class A(unittest.TestCase):
        def test_fails(self):
            harness.run("echo product")
            self.assertEqual(1, 2, "%s")
    """ % SECRET


class Hijack(RunnerTest):
    def plant(self):
        fake = f"""
            import json, os, sys
            print("Ran 1 test in 0.000s")
            print()
            print("OK")
            os.makedirs(sys.argv[sys.argv.index("--out") + 1], exist_ok=True)
            row = {{"id": "{self.card}.test_fails", "result": "pass"}}
            out = sys.argv[sys.argv.index("--out") + 1]
            open(out + "/results.jsonl", "w").write(json.dumps(row))
            """
        self.write("harness/__init__.py", "", self.ws)
        self.write("harness/unittest_runner.py", fake, self.ws)
        self.write("unittest.py", "raise SystemExit(0)\n", self.ws)
        self.write("json.py", "raise SystemExit(0)\n", self.ws)

    def test_a_product_planted_harness_or_stdlib_module_changes_nothing(self):
        """Intent row: the real runner runs and a failing test still fails."""
        self.write("test_a.py", FAILING)
        self.plant()
        proc, rows = self.runner()
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual([r["result"] for r in rows], ["fail"])
        self.assertIn("FAILED (failures=1)", proc.stdout)

    def test_the_cwd_is_what_protects_it(self):
        """Sanity: started from the workspace without the `cd /blind`, the planted one wins."""
        self.write("test_a.py", FAILING)
        self.plant()
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env[api.WORKSPACE_ENV] = str(self.ws)
        proc = subprocess.run(["python3", "-m", "harness.unittest_runner", "--card", self.card,
                               "--out", str(self.out), str(self.cards)], cwd=self.ws,
                              capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0)    # the fake ran: why the run line starts with cd

    def test_the_run_and_list_lines_start_with_cd_blind(self):
        for line in cli.RUN_LINES["unittest"]:
            self.assertTrue(line.startswith("cd /blind && exec python3 -m harness.unittest_runner"))


class Runner(RunnerTest):
    def test_mixed_card_rows_exit_code_and_summary_line_agree(self):
        self.write("test_a.py", f"""
            import unittest
            from harness import run

            class T(unittest.TestCase):
                def test_prints_version(self):
                    self.assertEqual(run("sh tool.sh").stdout, "version 1\\n")

                def test_wrong_value(self):
                    self.assertEqual(run("sh tool.sh").stdout, "{SECRET}")

                def test_boom_before_any_call(self):
                    raise RuntimeError("{SECRET}")

                @unittest.skip("later")
                def test_skipped(self):
                    pass
            """)
        proc, rows = self.runner()
        self.assertEqual(proc.returncode, 1, proc.stderr)
        by_id = {r["id"]: r for r in rows}
        self.assertEqual({k: v["result"] for k, v in by_id.items()},
                         {f"{self.card}.test_prints_version": "pass",
                          f"{self.card}.test_wrong_value": "fail",
                          f"{self.card}.test_boom_before_any_call": "error",
                          f"{self.card}.test_skipped": "skipped"})
        wrong = by_id[f"{self.card}.test_wrong_value"]
        self.assertEqual(wrong["observed"]["stdout_tail"], "version 1")
        self.assertNotIn("observed", by_id[f"{self.card}.test_boom_before_any_call"])
        ran, status = self.contract(proc, rows)
        self.assertEqual(ran, 4)
        for word in ("failures=1", "errors=1", "skipped=1"):
            self.assertIn(word, status)

    def test_stdout_carries_only_the_summary(self):
        self.write("test_a.py", f"""
            import unittest

            class T(unittest.TestCase):
                def test_wrong(self):
                    print("assertEqual(out, '{SECRET}') printed by the test")
                    self.assertEqual("a", "{SECRET}")
            """)
        proc, rows = self.runner()
        self.assertEqual(proc.returncode, 1)
        for text in (proc.stdout, (self.out / "results.jsonl").read_text()):
            for leak in (SECRET, "Traceback", "assertEqual", "File \""):
                self.assertNotIn(leak, text)
        self.assertEqual(set(rows[0]), {"id", "result"})
        self.assertEqual(len([x for x in proc.stdout.splitlines() if x.strip()]), 2)

    def test_all_pass_exits_zero(self):
        self.write("test_a.py", """
            import unittest
            from harness import run

            class T(unittest.TestCase):
                def test_ok(self):
                    self.assertEqual(run("exit 0").exit, 0)
            """)
        proc, rows = self.runner()
        self.assertEqual((proc.returncode, [r["result"] for r in rows]), (0, ["pass"]))
        self.assertEqual(self.contract(proc, rows), (1, "OK"))

    def test_observed_is_per_test_not_carried_over(self):
        self.write("test_a.py", """
            import unittest
            from harness import run

            class T(unittest.TestCase):
                def test_a_runs(self):
                    run("echo first")

                def test_b_does_not(self):
                    pass
            """)
        _, rows = self.runner()
        self.assertEqual({r["id"].split(".")[-1]: "observed" in r for r in rows},
                         {"test_a_runs": True, "test_b_does_not": False})

    def test_duplicate_test_names_are_refused_before_anything_runs(self):
        for mod in ("test_a.py", "test_b.py"):
            self.write(mod, """
                import unittest

                class T(unittest.TestCase):
                    def test_same(self):
                        pass
                """)
        proc, rows = self.runner()
        self.assertEqual((proc.returncode, rows), (2, []))
        self.assertIn("test_same", proc.stderr)

    def test_a_top_level_print_in_a_test_file_reaches_neither_run_nor_list_output(self):
        self.write("test_a.py", f"""
            import unittest
            print("{SECRET} printed at import")

            class T(unittest.TestCase):
                def test_one(self):
                    pass
            """)
        listed, _ = self.runner("--list")
        self.assertEqual((listed.returncode, listed.stdout), (0, f"{self.card}.test_one\n"))
        proc, rows = self.runner()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(SECRET, proc.stdout + proc.stderr)
        self.assertEqual(self.contract(proc, rows), (1, "OK"))

    def test_list_prints_ids_and_runs_nothing(self):
        self.write("test_a.py", """
            import unittest

            class T(unittest.TestCase):
                def test_one(self):
                    raise RuntimeError("would fail if run")
            """)
        proc, rows = self.runner("--list")
        self.assertEqual((proc.returncode, proc.stdout.strip(), rows),
                         (0, f"{self.card}.test_one", []))

    def test_scratch_dirs_are_removed_when_the_process_exits(self):
        done = subprocess.run(["python3", "-c", "import harness; print(harness.tmp())"],
                              cwd=self.blind, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(Path(done.stdout.strip()).exists())

    def test_the_package_is_stdlib_only_and_uses_relative_imports(self):
        source = "\n".join(p.read_text() for p in Path(harness.__file__).parent.glob("*.py"))
        self.assertNotRegex(source, r"(?m)^\s*(from|import)\s+capsule\b")


class IntentRows(unittest.TestCase):
    """The intent rows this side owns are executed by Hijack and Reconcile above; the loader test
    waits for the CoS-written contracts/intent/blind_harness.jsonl."""

    def test_loader_finds_this_sides_rows_once_the_file_exists(self):
        if not INTENT_FILE.exists():
            self.skipTest("contracts/intent/blind_harness.jsonl not written yet")
        ids = {json.loads(x)["id"] for x in INTENT_FILE.read_text().splitlines() if x.strip()}
        self.assertLessEqual(INTENT_IDS, ids)


if __name__ == "__main__":
    unittest.main()
