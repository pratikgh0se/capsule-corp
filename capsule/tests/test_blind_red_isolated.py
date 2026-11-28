"""R31: each run of the blind red check gets its own container and output folder
(`red._base_runs`, `red._one_run`): run 2 must not see what run 1 left (the home and temp folder, a
process, `/out`). Temp repos and a stand-in for the strong runner (`test_blind_run.Container`); no
docker, no model; the real-docker proof was a one-off outside the repo.

Z2 oracles: the runner's own record of its starts and stops (names, order, mounts, timeouts) and
the events the tick wrote (retry, landing)."""
import subprocess
from pathlib import Path
from unittest import mock

from capsule.blind import lint, red
from capsule.blind import run as blind_run
from capsule.tests.test_blind_red_tick import RedBox, RedCase

TESTER = "capsule-000000000000"      # the tester's container, stopped first by a whole pass


class Clock:
    """`red.time` with a hand-set `monotonic`."""

    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now


class Fresh(RedBox):
    """A stand-in container that does not exist any more once stopped, and whose name only one
    run may have: a second run in the same name, or two live at once, fails the run."""

    def __init__(self, clock=None, **kw):
        super().__init__(**kw)
        self.clock = clock

    def start(self, run_id, ws, **kw):
        name = "capsule-" + run_id[:12]
        assert name not in ["capsule-" + s[0][:12] for s in self.started], name
        return super().start(run_id, ws, **kw)

    def exec(self, name, cmd, timeout):
        assert name not in self.stopped, f"{name} was already stopped"
        live = len(self.started) - len([s for s in self.stopped if s != TESTER])
        assert live == 1, "containers overlap or are left"
        return super().exec(name, cmd, timeout)


class IsolatedRuns(RedCase):
    def base_runs(self, box, deadline=None):
        """`red._base_runs` on the card's staged tests, with `box` as the strong runner."""
        clone = self.make_round()
        files, _ = lint.collect(clone, self.card)
        commit = red._stage(self.repo, self.card, files, "scratch")
        self.tick.strong = box
        task = {"id": self.card, "branch_name": "main"}
        return red._base_runs(self.tick, self.cfg, task, self.repo, commit, self.base,
                              deadline or red.time.monotonic() + 300)

    def base_runs_on(self, name, box):
        clone = self.make_round()
        files, _ = lint.collect(clone, self.card)
        commit = red._stage(self.repo, self.card, files, "scratch")
        self.tick.strong = box
        task = {"id": self.card, "branch_name": name}
        return red._base_runs(self.tick, self.cfg, task, self.repo, commit, self.base,
                              red.time.monotonic() + 300)

    def test_a_branch_name_git_refuses_is_refused_before_any_container_starts(self):
        for name in ("feat/.hidden", "a//b", "a@{b"):
            with self.subTest(name=name):
                box = Fresh()
                with self.assertRaises(ValueError):
                    self.base_runs_on(name, box)
                self.assertEqual(box.started, [])

    def test_a_valid_branch_name_still_runs(self):
        self.assertEqual(len(self.base_runs_on("feat/x", Fresh())), red.RUNS)

    def clocked(self, clock):
        patcher = mock.patch.object(red, "time", clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_run_has_its_own_container_and_output_folder_and_the_last_is_stopped(self):
        self.make_round()
        box = Fresh()
        self.step(box)
        self.assertEqual(len(self.events("blind_landed")), 1)
        names = ["capsule-" + s[0][:12] for s in box.started]
        self.assertEqual((len(set(names)), box.stopped[1:]), (red.RUNS, names))
        outs = {s[2]["mounts"][1] for s in box.started}
        self.assertEqual(len(outs), red.RUNS)
        self.assertFalse(any(Path(o.split(":")[0]).exists() for o in outs))

    def test_a_raise_in_run_two_stops_both_containers_and_removes_everything(self):
        class Boom(Fresh):
            def exec(self, name, cmd, timeout):
                if self.execs == 1:
                    raise RuntimeError("docker is gone")
                return super().exec(name, cmd, timeout)

        self.make_round()
        box = Boom()
        self.step(box)
        self.assertEqual((len(box.started), box.execs), (2, 1))
        self.assertEqual(box.stopped[1:], ["capsule-" + s[0][:12] for s in box.started])
        self.assertEqual([e["attempt"] for e in self.events("blind_red_retry")], [1])
        self.assertEqual((list((self.root / "spool").glob("*/*")), self.events("blind_landed")),
                         ([], []))

    def test_a_run_that_times_out_ends_the_check_as_a_retry_with_its_container_stopped(self):
        class Slow(Fresh):
            def exec(self, name, cmd, timeout):
                if self.execs == 2:
                    self.execs += 1
                    raise subprocess.TimeoutExpired(cmd, timeout)
                return super().exec(name, cmd, timeout)

        self.make_round()
        box = Slow()
        self.step(box)
        self.assertEqual((len(box.started), len(box.stopped) - 1), (3, 3))
        self.assertEqual(len(self.events("blind_red_retry")), 1)
        self.assertEqual(self.events("blind_landed"), [])

    def test_the_results_are_read_after_the_container_is_stopped(self):
        events = []

        class Ordered(Fresh):
            def stop(self, name):
                events.append("stop")
                super().stop(name)

        real = blind_run._read
        def spy(path):
            if Path(path).name == blind_run.RESULTS_FILE:
                events.append("read")
            return real(path)

        with mock.patch.object(blind_run, "_read", spy):
            self.base_runs(Ordered())
        self.assertEqual(events, ["stop", "read"] * red.RUNS)

    def test_a_budget_that_runs_out_between_runs_stops_the_check_before_the_next_start(self):
        clock = Clock()
        self.clocked(clock)

        class Burn(Fresh):
            def exec(self, name, cmd, timeout):
                out = super().exec(name, cmd, timeout)
                self.clock.now = 1000                  # run 1 spends the whole budget
                return out

        box = Burn(clock)
        with self.assertRaises(TimeoutError):
            self.base_runs(box, 500)
        self.assertEqual(len(box.started), 1)
        self.assertEqual(box.stopped, ["capsule-" + box.started[0][0][:12]])

    def test_the_time_left_for_a_run_is_worked_out_after_its_container_started(self):
        clock = Clock()
        self.clocked(clock)

        class SlowStart(Fresh):
            def start(self, run_id, ws, **kw):
                self.clock.now += 50
                return super().start(run_id, ws, **kw)

        box = SlowStart(clock)
        self.base_runs(box, 500)
        self.assertEqual(box.timeouts, [450, 400, 350, 300, 250])

    def test_every_docker_call_of_the_runs_is_bounded_by_the_one_deadline(self):
        clock = Clock()
        self.clocked(clock)

        class Hooked(Fresh):
            def __init__(self, clock):
                super().__init__(clock)
                self.passed = []
                self._run = lambda cmd, **kw: self.passed.append(kw.get("timeout"))
                self._sleep = lambda secs: None

            def start(self, run_id, ws, **kw):
                self._run(["docker", "run"], timeout=900)       # a long call: cut to the time left
                return super().start(run_id, ws, **kw)

        box = Hooked(clock)
        hooks = (box._run, box._sleep)
        self.base_runs(box, 500)
        self.assertEqual(box.passed, [500] * red.RUNS)
        self.assertEqual((box._run, box._sleep), hooks)         # and put back

    def test_each_run_has_a_spool_of_its_own_with_a_private_blind_out_folder(self):
        """`<spool root>/<run id>/blind-out`: the shape a blind run has, whatever the check is;
        no run number, no folder two runs share."""
        seen = []

        class Peek(Fresh):
            def start(self, run_id, ws, **kw):
                out = Path(kw["mounts"][1].split(":")[0])
                seen.append((run_id, out.name, out.parent, out.stat().st_mode & 0o777))
                return super().start(run_id, ws, **kw)

        self.base_runs(Peek())
        spool = self.root / "spool"
        self.assertEqual([(name, parent.parent, mode) for _, name, parent, mode in seen],
                         [(blind_run.OUT_DIR, spool, 0o700)] * red.RUNS)
        self.assertEqual([parent.name for _, _, parent, _ in seen], [r[0] for r in seen])
        self.assertEqual(len({r[0] for r in seen}), red.RUNS)
