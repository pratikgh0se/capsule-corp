"""`contract_shards`: expansion, every module exactly once, and the lander's use of it."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import check_gate, config, lander, shards, test_shard, workspace

CMD = "python3 -m unittest discover -s capsule/tests -t ."
CFG = {"contract_commands": [CMD, "echo other"], "contract_shards": {CMD: 3}}


class Expand(unittest.TestCase):
    def test_a_sharded_command_becomes_n_named_parts(self):
        got = shards.expand(CFG)
        self.assertEqual(len(got), 4)
        self.assertEqual(got[0], "python3 -m capsule.test_shard 1 3 -s capsule/tests -t ."
                                 " # part 1/3")
        self.assertTrue(got[2].endswith("# part 3/3"))
        self.assertEqual(got[3], "echo other")

    def test_without_the_setting_nothing_changes(self):
        cfg = {"contract_commands": [CMD, "echo other"]}
        self.assertEqual(shards.expand(cfg), cfg["contract_commands"])

    def test_the_setting_is_validated(self):
        flagged = {CMD + " -p 'x*.py'": 3}
        for bad in ([], {"echo x": 3}, {CMD: 1}, {CMD: True}, {CMD: "3"}, flagged,
                    {CMD + " -v": 3}, {"python3 -m unittest discover -s a -s b": 3}):
            with self.assertRaises(ValueError):
                shards.validate({"contract_commands": [CMD], "contract_shards": bad})
        shards.validate({"contract_commands": [CMD], "contract_shards": {CMD: 2}})


class Coverage(unittest.TestCase):
    def test_every_module_runs_in_exactly_one_part(self):
        with tempfile.TemporaryDirectory() as top:
            tests = Path(top, "tests", "sub")
            tests.mkdir(parents=True)
            for i in range(11):
                (tests.parent / f"test_m{i}.py").write_text("")
            (tests / "test_deep.py").write_text("")
            (tests / "helper.py").write_text("")
            (tests.parent / "tests_extra.py").write_text("")   # discover's default `test*.py`
            names = test_shard.share(Path(top, "tests"), top, 1, 1)[0]
            self.assertIn("tests.tests_extra", names)
            self.assertEqual(len(names), 13)
            for n in (1, 5, 12, 20):
                parts = [test_shard.share(Path(top, "tests"), top, k, n)[0]
                         for k in range(1, n + 1)]
                self.assertEqual(sorted(m for p in parts for m in p), names)


class FailClosed(unittest.TestCase):
    def run_main(self, start, k=1, n=2):
        with mock.patch("sys.stdout"), mock.patch("sys.path", [str(Path(start).parent), *sys.path]):
            return test_shard.main([str(k), str(n), "-s", start, "-t", str(Path(start).parent)])

    def test_a_missing_or_empty_folder_fails(self):
        with tempfile.TemporaryDirectory() as top:
            self.assertEqual(self.run_main(str(Path(top, "gone"))), 1)
            Path(top, "empty").mkdir()
            self.assertEqual(self.run_main(str(Path(top, "empty"))), 1)

    def test_a_share_that_ran_no_test_fails_but_an_empty_share_of_many_passes(self):
        with tempfile.TemporaryDirectory() as top:
            d = Path(top, "tnone")
            d.mkdir()
            (d / "test_a.py").write_text("")          # a module with no test in it
            self.assertEqual(self.run_main(str(d), 1, 2), 1)
            self.assertEqual(self.run_main(str(d), 2, 2), 0)    # part 2 has no module: fine

    def test_a_passing_share_passes(self):
        with tempfile.TemporaryDirectory() as top:
            d = Path(top, "tpass")
            d.mkdir()
            (d / "test_a.py").write_text("import unittest\n"
                                         "class T(unittest.TestCase):\n"
                                         "    def test_x(self): pass\n")
            self.assertEqual(self.run_main(str(d), 1, 1), 0)


def rows(over=None, n=12):
    """A passing contract output per part; `over` maps a part number to a row change."""
    out = []
    for cmd in (c for c in shards.expand({**CFG, "contract_shards": {CMD: n}}) if "# part" in c):
        k = int(cmd.rsplit("part ", 1)[1].split("/")[0])
        row = {"cmd": cmd, "exit": 0, "output": f"part {k}/{n}: ran 5 of 60 test modules"}
        out.append({**row, **(over or {}).get(k, {})})
    return out


class PartsGate(unittest.TestCase):
    cfg = {"contract_commands": [CMD], "contract_shards": {CMD: 12}}

    def test_all_parts_passing_is_clean(self):
        self.assertEqual(shards.failures(shards.expand(self.cfg), rows()), [])

    def test_a_failed_part_fails_even_if_the_rest_passed(self):
        got = shards.failures(shards.expand(self.cfg), rows({7: {"exit": 1}}))
        self.assertEqual(len(got), 1)
        self.assertIn("part 7/12 exited 1", got[0])

    def test_a_timed_out_part_is_named_as_a_timeout(self):
        got = shards.failures(shards.expand(self.cfg), rows({3: {"exit": 124}}))
        self.assertIn("part 3/12 timed out", got[0])

    def test_a_missing_part_fails(self):
        kept = [r for r in rows() if "# part 7/" not in r["cmd"]]
        got = shards.failures(shards.expand(self.cfg), kept)
        self.assertEqual(len(got), 1)
        self.assertIn("part 7/12 missing", got[0])

    def test_different_module_counts_fail(self):
        odd = {4: {"output": "part 4/12: ran 5 of 61 test modules"}}
        got = shards.failures(shards.expand(self.cfg), rows(odd))
        self.assertTrue(any("different module counts" in x for x in got))

    def test_the_count_survives_the_output_tail_cut(self):
        with tempfile.TemporaryDirectory() as top:
            d = Path(top, "tcount")
            d.mkdir()
            (d / "test_a.py").write_text("import sys, unittest\n"
                                         "class T(unittest.TestCase):\n"
                                         "    def test_x(self): sys.stderr.write('E' * 20000)\n")
            env = {**os.environ, "PYTHONPATH": os.pathsep.join([top, os.getcwd()])}
            out = subprocess.run([sys.executable, "-m", "capsule.test_shard", "1", "1", "-s",
                                  str(d), "-t", top], capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr[-300:])
        saved = (out.stdout + out.stderr)[-8000:]      # what the contract job keeps
        self.assertGreater(len(out.stdout + out.stderr), 8000)
        self.assertEqual(shards._FINAL.findall(saved)[-1], "1")

    def test_a_part_with_only_its_header_fails(self):
        head = {2: {"output": "part 2/12: selected 5 of 60 test modules"}}
        got = shards.failures(shards.expand(self.cfg), rows(head))
        self.assertIn("part 2/12 did not report its module count", got[0])

    def test_the_lander_suites_check_fails_a_silent_part(self):
        parts = shards.expand({"contract_commands": [CMD], "contract_shards": {CMD: 2}})
        silent = [(0, "part 1/2: selected 5 of 9 test modules"),
                  (0, "part 2/2: ran 4 of 9 test modules")]
        box = mock.Mock(side_effect=lambda cmd, t: silent.pop(0))
        with mock.patch.object(lander, "_tail", side_effect=lambda rc, out: (rc, out)):
            status, detail = lander._suites(mock.Mock(), [CMD], [], 5, box, parts=parts)
        self.assertEqual(status, "fail")
        self.assertIn("part 1/2 did not report", detail)

    def test_the_lander_retry_also_needs_every_count_line(self):
        parts = shards.expand({"contract_commands": [CMD], "contract_shards": {CMD: 2}})
        runs = [(0, "part 1/2: ran 4 of 9 test modules"), (1, "FAILED"),
                (0, "part 1/2: selected 4 of 9 test modules"),
                (0, "part 2/2: ran 5 of 9 test modules")]
        box = mock.Mock(side_effect=lambda cmd, t: runs.pop(0))
        with mock.patch.object(lander, "_tail", side_effect=lambda rc, out: (rc, out)):
            status, detail = lander._suites(mock.Mock(), [CMD], [], 5, box, parts=parts)
        self.assertEqual(status, "fail")
        self.assertIn("part 1/2 did not report", detail)

    def test_without_the_setting_a_part_looking_command_is_plain(self):
        cmd = "echo go # part of nightly"
        box = mock.Mock(return_value=(0, "ok"))
        with mock.patch.object(lander, "_tail", side_effect=lambda rc, out: (rc, out)):
            self.assertEqual(lander._suites(mock.Mock(), [cmd], [], 5, box)[0], "pass")

    def test_duplicate_commands_are_refused_with_shards(self):
        cfg = {"contract_commands": [CMD, CMD], "contract_shards": {CMD: 2}}
        with self.assertRaisesRegex(ValueError, "duplicate"):
            shards.validate(cfg)
        shards.validate({"contract_commands": [CMD, CMD]})     # no setting: unchanged

    def test_no_setting_no_failures(self):
        self.assertEqual(shards.failures(shards.expand({"contract_commands": [CMD]}), []), [])

    def test_check_gate_reads_the_validator_jobs_outputs(self):
        with tempfile.TemporaryDirectory() as spool:
            Path(spool, "r1").mkdir()
            Path(spool, "r1", "job.json").write_text(
                json.dumps({"inputs": {"outputs": rows({7: {"exit": 1}})}}))
            got = check_gate.failures(self.cfg, None, spool, {"run_id": "r1", "task_id": "t"})
            self.assertEqual(len(got), 1)
            self.assertIn("part 7/12 exited 1", got[0])


class Protected(unittest.TestCase):
    def test_the_shard_runner_and_split_are_refused(self):
        paths = ["capsule/test_shard.py", "capsule/shards.py", "capsule/test_shard_x.py"]
        self.assertEqual(sorted(workspace.touches_protected(paths)), sorted(paths))

    def test_a_typo_key_does_not_validate(self):
        cfg = {"contract_commands": [CMD], "contract_shards": {CMD + "x": 3}}
        with self.assertRaisesRegex(ValueError, "not one of contract_commands"):
            shards.validate(cfg)


class NeedsDockerHold(unittest.TestCase):
    def test_a_sharded_docker_command_still_holds(self):
        status, _ = lander._suites(mock.Mock(), [CMD], [CMD], 5, mock.Mock(),
                                   parts=shards.expand(CFG))
        self.assertEqual(status, "hold")


class AllPartsMustPass(unittest.TestCase):
    def run_parts(self, rcs):
        calls = iter(rcs)
        box = mock.Mock(side_effect=lambda cmd, t: (next(calls), f"out {cmd}"))
        work = mock.Mock()
        with mock.patch.object(lander, "_tail", side_effect=lambda rc, out: (rc, out)):
            return lander._commands(shards.expand(CFG), work, 5, box=box)

    def test_all_parts_green_passes(self):
        self.assertEqual(self.run_parts([0, 0, 0, 0])[0], "pass")

    def test_a_failed_part_fails_and_is_named(self):
        status, detail = self.run_parts([0, 1, 0, 0])
        self.assertEqual(status, "fail")
        self.assertIn("# part 2/3", detail)

    def test_config_validates_the_setting(self):
        path = Path(config.__file__).parent / "config.capsule-corp.example.json"
        cfg = json.loads(path.read_text())
        config.validate(cfg)                  # the shipped example, sharded, is valid
        self.assertIn(CMD, cfg["contract_shards"])
        cfg["contract_shards"][CMD] = 1
        with self.assertRaisesRegex(ValueError, "contract_shards"):
            config.validate(cfg)


if __name__ == "__main__":
    unittest.main()
