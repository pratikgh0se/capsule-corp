"""Throwaway postgres in tests/*_pg.sh must not leak Docker volumes.

The postgres image declares a VOLUME, so a container started without --rm leaves an anonymous
volume behind on every run (115 piled up, 5.3 GB). maintenance/storage_gc.py only removes
volumes labelled capsule=1, so each `docker run` here needs --rm, a capsule=1 label on the
container and on its data volume, and the suite must `docker stop` it from an EXIT trap.
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SUITES = sorted((REPO / "tests").glob("*_pg.sh"))
RUN = re.compile(r"\bdocker run\b(?:[^\n]*\\\n)*[^\n]*")
TRAP = re.compile(r"^\s*trap [^\n]*EXIT", re.M)
NAME = re.compile(r'^CT="capsule-pgtest-[a-z-]+-\$\$"', re.M)
REQUIRED = ("--rm", "--label capsule=1", "volume-label=capsule=1")


class PgSuiteVolumes(unittest.TestCase):
    def test_suites_found(self):
        self.assertTrue(SUITES, "no tests/*_pg.sh found")

    def test_docker_run_is_removable_and_labelled(self):
        for suite in SUITES:
            text = suite.read_text()
            runs = RUN.findall(text)
            self.assertTrue(runs, f"{suite.name}: no docker run found")
            for run in runs:
                for flag in REQUIRED:
                    self.assertIn(flag, run, f"{suite.name}: docker run lacks {flag}")

    def test_exit_trap_stops_the_container(self):
        for suite in SUITES:
            text = suite.read_text()
            trap, run = TRAP.search(text), RUN.search(text)
            self.assertTrue(trap, f"{suite.name}: no EXIT trap")
            self.assertLess(trap.start(), run.start(), f"{suite.name}: trap set after docker run")
            self.assertIn('docker stop "$CT"', text, f"{suite.name}: no docker stop")

    def test_container_name_is_outside_the_compose_prefix(self):
        # storage_gc treats capsule-corp-* as the compose stack and never cleans it.
        for suite in SUITES:
            self.assertRegex(suite.read_text(), NAME, f"{suite.name}: CT not capsule-pgtest-*")


if __name__ == "__main__":
    unittest.main()
