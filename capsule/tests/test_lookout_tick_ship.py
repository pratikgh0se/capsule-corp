"""The tick launches the real lookout shipper detached and never waits for it (I2, second half)."""
import contextlib
import json
import os
import time
import unittest

from capsule import lookout
from capsule.tests import test_tick as tt
from capsule.tests.test_lookout_ship import FAKEBIN, card


class TickNeverWaits(tt.TickCase):
    def test_a_slow_shipper_does_not_slow_the_pass(self):
        """I2 (second half): the pass that launches a 5 s shipper returns well inside 5 s."""
        os.environ.update(AGENT_OPS_DOCKER_BIN=str(FAKEBIN), FAKE_PSQL_SLEEP="5", FAKE_PSQL_RC="0")
        for k in ("AGENT_OPS_DOCKER_BIN", "FAKE_PSQL_SLEEP", "FAKE_PSQL_RC"):
            self.addCleanup(os.environ.pop, k, None)
        self.cfg["lookout"] = {"ship": True}
        self.write_cfg()
        cap = self.home / "captured.sql"
        os.environ["FAKE_PSQL_CAPTURE"] = cap.as_posix()
        self.addCleanup(os.environ.pop, "FAKE_PSQL_CAPTURE", None)
        lookout.lookout_dir(self.home).mkdir(parents=True, exist_ok=True)
        with open(lookout.spool_path(self.home), "a", encoding="utf-8") as f:
            f.write(json.dumps(card("slow-ship-card")) + "\n")
        self.tick.lookout_kick = lookout.kick     # the real launch (TickCase stubs it)
        t0 = time.monotonic()
        self.tick.once()
        elapsed = time.monotonic() - t0
        pid_dir = lookout.lookout_dir(self.home)
        pids = {n: int((pid_dir / f"{n}.pid").read_text()) for n in ("ship", "check")}
        for pid in pids.values():          # the pass launched both detached children: reap each
            self.addCleanup(self._kill_session, pid)
        self.assertLess(elapsed, 3.0)
        for name, pid in pids.items():
            self.assertTrue(self._reaped(pid, 30), f"the {name} child did not finish")
        self.assertIn("slow-ship-card", cap.read_text(encoding="utf-8"))   # it shipped the row

    @staticmethod
    def _reaped(pid, timeout):
        """True once the child `pid` has exited and been reaped, polling up to `timeout` s."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if os.waitpid(pid, os.WNOHANG)[0]:
                    return True
            except ChildProcessError:
                return True
            time.sleep(0.1)
        return False

    @staticmethod
    def _kill_session(pid):
        with contextlib.suppress(ProcessLookupError, ChildProcessError, PermissionError):
            os.killpg(pid, 9)
            os.waitpid(pid, 0)


if __name__ == "__main__":
    unittest.main()
