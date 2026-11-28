"""t_53c8a81a: obs_probe says whether the worker image matches the capsule-python row of the
image record ($CAPSULE_HOME/images.lock), before anyone starts a card on it. Oracles: the stdout
line `main` prints (checked against the digests the stub and the scratch lock were built from) and
the exit code (checked against a run with the same stub minus the lock). A stubbed subprocess call
only; no test reaches a real docker."""
import io
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import obs_image_lock  # noqa: E402
import obs_probe  # noqa: E402

LOCK_HEX = "ab" * 32
OTHER_HEX = "cd" * 32
IMAGE = "capsule-corp-worker:test-pin"


def lock_file(body):
    path = Path(tempfile.mkdtemp(prefix="obs-lock-")) / "IMAGES.lock"
    path.write_text(body)
    return path


def stub(out="", rc=0, err="", raises=None):
    """A `run` that answers every docker call with one canned result; records argv."""
    calls = []

    def run(argv, **kw):
        calls.append(list(argv))
        if raises:
            raise raises
        return subprocess.CompletedProcess(argv, rc, out, err)
    run.calls = calls
    return run


class Check(unittest.TestCase):
    BOTH_ROWS = f"capsule-base@sha256:{OTHER_HEX}\ncapsule-python@sha256:{LOCK_HEX}\n"

    def check(self, run, body=BOTH_ROWS):
        return obs_image_lock.check(run, lock=lock_file(body), image=IMAGE)

    def test_matching_digests_report_ok(self):
        state, line = self.check(stub(f"sha256:{LOCK_HEX}\n"))
        self.assertEqual(state, "ok")
        self.assertIn(LOCK_HEX[:12], line)

    def test_a_different_digest_reports_a_one_line_mismatch_with_both_short_digests(self):
        run = stub(f"sha256:{OTHER_HEX}\n")
        state, line = self.check(run)
        self.assertEqual(state, "mismatch")
        self.assertNotIn("\n", line)
        self.assertIn(OTHER_HEX[:12], line)
        self.assertIn(LOCK_HEX[:12], line)
        self.assertNotIn(LOCK_HEX[:13], line)
        self.assertEqual(run.calls[0][1:], ["image", "inspect", "--format", "{{.Id}}", IMAGE])

    def test_docker_trouble_is_n_a_not_a_failure(self):
        for name, run in (("daemon down", stub(rc=1, err="Cannot connect to the Docker daemon")),
                          ("not installed", stub(raises=FileNotFoundError("docker"))),
                          ("timeout", stub(raises=subprocess.TimeoutExpired("docker", 30))),
                          ("junk output", stub("not a digest\n"))):
            with self.subTest(name):
                self.assertEqual(self.check(run)[0], "n/a")

    def test_a_lock_without_a_python_row_is_n_a_and_docker_is_not_called(self):
        for body in (f"capsule-base@sha256:{OTHER_HEX}\n", "# only comments\n",
                     "capsule-python:local\n"):
            with self.subTest(body=body):
                run = stub(f"sha256:{LOCK_HEX}\n")
                self.assertEqual(self.check(run, body)[0], "n/a")
                self.assertEqual(run.calls, [])

    def test_a_missing_lock_file_is_n_a(self):
        missing = Path(tempfile.mkdtemp(prefix="obs-lock-")) / "none"
        state, _ = obs_image_lock.check(stub(f"sha256:{LOCK_HEX}\n"), lock=missing, image=IMAGE)
        self.assertEqual(state, "n/a")

    def test_only_a_capsule_python_at_or_colon_row_counts_not_a_longer_name(self):
        body = self.BOTH_ROWS + f"capsule-pythonic@sha256:{OTHER_HEX}\n"
        self.assertEqual(self.check(stub(f"sha256:{LOCK_HEX}\n"), body)[0], "ok")


class RecordImage(unittest.TestCase):
    """The check inspects the image a board would run (docker.worker_image) and compares it with
    the record $CAPSULE_HOME/images.lock, never the tracked lock; OPS_WORKER_IMAGE is honoured."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="obs-board-"))
        self.capsule_home = tmp / "capsule"
        self.capsule_home.mkdir()
        env = {"CAPSULE_HOME": str(self.capsule_home), "OPS_ENV_FILE": str(tmp / "no-ops.env")}
        patcher = unittest.mock.patch.dict("os.environ", env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("OPS_WORKER_IMAGE", None)   # restored by the patch.dict cleanup

    def record(self, row):
        (self.capsule_home / "images.lock").write_text(row + "\n")

    def test_the_record_row_is_the_image_inspected_and_the_reference(self):
        mine = f"capsule-python@sha256:{OTHER_HEX}"
        self.record(mine)
        run = stub(f"sha256:{OTHER_HEX}\n")
        self.assertEqual(obs_image_lock.check(run)[0], "ok")
        self.assertEqual(run.calls[0][-1], mine)
        # docker says some other digest: that is not the record's image
        state, line = obs_image_lock.check(stub(f"sha256:{LOCK_HEX}\n"))
        self.assertEqual(state, "mismatch", line)

    def test_no_record_is_n_a_never_ok(self):
        run = stub(f"sha256:{LOCK_HEX}\n")      # the tracked lock's digest would say ok
        self.assertEqual(obs_image_lock.check(run)[0], "n/a")
        self.assertEqual(run.calls, [])

    def test_ops_worker_image_override_is_the_image_inspected(self):
        self.record(f"capsule-python@sha256:{LOCK_HEX}")
        os.environ["OPS_WORKER_IMAGE"] = "capsule-corp-worker:operator-pin"
        run = stub(f"sha256:{LOCK_HEX}\n")
        self.assertEqual(obs_image_lock.check(run)[0], "ok")
        self.assertEqual(run.calls[0][-1], "capsule-corp-worker:operator-pin")


class ProbeMain(unittest.TestCase):
    """main() prints the line and keeps its exit code whatever the image check says."""

    def main(self, run, body):
        lock = lock_file(body)
        out = io.StringIO()
        samples = [obs_probe._sample(obs_probe._now(None), "capsule.host.load1", 1.0)]

        def collect(run_, home, state_dir, logs_dir, now=None, status=None):
            status.extend([("host", True)])
            return samples
        shipped = obs_probe.ShipResult(True, "")
        with unittest.mock.patch.object(obs_probe, "collect", collect), \
                unittest.mock.patch.object(obs_probe, "ship", lambda *a, **k: shipped), \
                unittest.mock.patch.object(obs_image_lock.docker.paths, "image_record",
                                           lambda home_=None: lock), \
                unittest.mock.patch.object(obs_image_lock, "_image", lambda: IMAGE), \
                unittest.mock.patch("sys.stdout", out):
            rc = obs_probe.main(run=run, home=Path("/nonexistent"), state_dir=Path("/nonexistent"),
                                logs_dir=Path("/nonexistent"))
        return rc, out.getvalue()

    GOOD = f"capsule-python@sha256:{LOCK_HEX}\n"

    def test_mismatch_is_printed_and_the_exit_code_is_unchanged(self):
        rc_mismatch, out = self.main(stub(f"sha256:{OTHER_HEX}\n"), self.GOOD)
        self.assertIn("worker image mismatch", out)
        self.assertIn(OTHER_HEX[:12], out)
        rc_ok, out_ok = self.main(stub(f"sha256:{LOCK_HEX}\n"), self.GOOD)
        self.assertIn("worker image ok", out_ok)
        self.assertEqual((rc_mismatch, rc_ok), (0, 0))

    def test_a_docker_error_prints_n_a_and_the_exit_code_is_unchanged(self):
        rc, out = self.main(stub(rc=1, err="Cannot connect to the Docker daemon"), self.GOOD)
        self.assertIn("worker image n/a", out)
        self.assertEqual(rc, 0)
        self.assertIn("obs_probe: 1 samples, 1/1 collectors ok", out)

    def test_an_unexpected_error_in_the_check_prints_n_a_and_cannot_change_the_exit_code(self):
        def boom(*a, **k):
            raise KeyError("secret-looking detail")
        with unittest.mock.patch.object(obs_image_lock, "check", boom):
            rc, out = self.main(stub(f"sha256:{LOCK_HEX}\n"), self.GOOD)
        self.assertIn("obs_probe: worker image n/a: KeyError\n", out)
        self.assertNotIn("secret-looking detail", out)
        self.assertEqual(rc, 0)
        self.assertIn("obs_probe: 1 samples, 1/1 collectors ok", out)


if __name__ == "__main__":
    unittest.main()
