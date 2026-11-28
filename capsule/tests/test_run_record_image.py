"""Follow-up to t_73573060: start_state.json's `runner_image` is the image the run's container
really ran: the image recorded in $CAPSULE_HOME/images.lock, or the operator's override.
Oracles: the raw record row read from disk, and the start_state.json a real tick + worker run
leaves in the spool."""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from capsule import run_record
from capsule.runners import docker, sandbox, scoped
from capsule.tests.test_tick import IMPL_DONE, FakeRunner, TickCase

MINE = "capsule-python@sha256:" + "ef" * 32


class StrongFake(FakeRunner, docker.StrongRunner):
    """A strong runner that starts no container: the tick treats it as the docker tier."""


class RecordFake(StrongFake):
    """The real `StrongRunner.available` over a docker that answers every call, and a runner that
    starts no container: what the tick sees on a board."""
    available = docker.StrongRunner.available

    def __init__(self):
        FakeRunner.__init__(self)
        self.docker = "docker"
        self._run = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "28.0\n", "")


class RecordBoardTick(TickCase):
    """With no image built the strong tier is down and the card waits (never `docker run ''`,
    never a card stuck running); it starts once one is."""
    MINE = MINE

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"OPS_ENV_FILE": "/nonexistent/ops.env"})
        env.start()
        self.addCleanup(env.stop)
        for name in ("OPS_WORKER_IMAGE", "AGENT_OPS_NO_DOCKER"):
            os.environ.pop(name, None)
        self.runner = RecordFake()
        self.script["implementer"] = [[IMPL_DONE]]

    def passes(self, n=3):
        tick = self.make_tick()
        for _ in range(n):
            tick.once()
            if self.runs("implementer"):
                break

    def test_no_record_means_the_card_waits_untouched_and_runs_once_one_appears(self):
        self.passes()
        self.assertEqual(self.runs(), [])                       # no run, so no crash incident
        self.assertEqual(self.task()["status"], "ready")
        self.assertEqual([c for c in self.runner.calls if c[0] == "start"], [])
        (self.home / "images.lock").write_text(self.MINE + "\n")
        self.passes()
        self.assertTrue(self.runs("implementer"))
        self.assertEqual(self.runner.started[0][0], self.runs("implementer")[0]["run_id"])

    def test_the_tick_reads_its_own_home_not_the_one_in_the_environment(self):
        elsewhere = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, elsewhere, True)
        (self.home / "images.lock").write_text(self.MINE + "\n")
        with mock.patch.dict(os.environ, {"CAPSULE_HOME": str(elsewhere)}):   # holds no record
            self.passes()
        self.assertTrue(self.runs("implementer"))

    def test_the_runner_and_the_landing_sandbox_are_given_the_home(self):
        made = scoped(lambda tier, role: FakeRunner(), lambda: {"board": "b"}, home=self.home)
        self.assertEqual(made("strong", "implementer").home, self.home)
        got = []

        def strong(**kw):
            got.append(kw)
            return mock.Mock(available=lambda: True)
        with mock.patch.object(sandbox.docker, "StrongRunner", strong), \
                mock.patch.object(sandbox, "_start", lambda *a: (None, "no")), \
                tempfile.TemporaryDirectory() as src, \
                sandbox.REAL_SESSION(Path(src), self.cfg, 5, home=self.home):
            pass
        self.assertEqual(got[0]["home"], self.home)


class RunnerImageRecorded(TickCase):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"OPS_ENV_FILE": "/nonexistent/ops.env"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("OPS_WORKER_IMAGE", None)
        self.runner = StrongFake()
        (self.home / "images.lock").write_text(MINE + "\n")

    def run_implementer(self):
        self.tick = self.make_tick()
        self.script["implementer"] = [[IMPL_DONE]]
        for _ in range(3):
            self.tick.once()
            if self.runs("implementer"):
                break
        sp = self.spool_dir(self.runs("implementer")[0]["run_id"])
        return (json.loads((sp / "job.json").read_text()),
                json.loads((sp / "start_state.json").read_text()))

    def test_the_recorded_image_is_what_the_container_ran(self):
        job, state = self.run_implementer()
        self.assertEqual(job["runner_image"], MINE)
        self.assertEqual(state["runner_image"], MINE)

    def test_an_operator_override_is_what_gets_recorded(self):
        os.environ["OPS_WORKER_IMAGE"] = "capsule-corp-worker:operator-pin"
        job, state = self.run_implementer()
        self.assertEqual((job["runner_image"], state["runner_image"]),
                         ("capsule-corp-worker:operator-pin",) * 2)

    def test_no_container_started_or_a_non_docker_runner_gets_no_image_key(self):
        strong = StrongFake()
        with mock.patch.dict(os.environ, {"CAPSULE_HOME": str(self.home)}):
            self.assertEqual(run_record.job_image(strong, started=False), {})
            self.assertEqual(run_record.job_image(strong, started=True), {"runner_image": MINE})
            self.assertEqual(run_record.job_image(FakeRunner(), started=True), {})

    def test_the_image_recorded_is_the_one_built_here_not_the_repo_lock(self):
        strong = StrongFake()
        with mock.patch.dict(os.environ, {"CAPSULE_HOME": str(self.home)}):
            (self.home / "images.lock").write_text("")
            self.assertEqual(run_record.job_image(strong, started=True), {"runner_image": ""})
