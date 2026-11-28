"""W208 intent: the tick launches `lookout check` at most hourly per home (two boards share one
launch), whatever `lookout.ship` says, and the launch records its pid so single-flight works."""
import os
import subprocess
import types
import unittest
from unittest import mock

from capsule import lookout
from capsule.tests import test_tick as tt   # module import: its TestCases are not re-collected here


class CheckWiring(tt.TickCase):
    def test_check_launches_once_an_hour_and_records_its_pid_even_with_shipping_off(self):
        self.cfg["lookout"] = {"ship": False}
        self.write_cfg()
        self.tick.lookout_kick = lookout.kick
        popen = mock.Mock()
        fake = types.SimpleNamespace(**{**vars(subprocess), "Popen": popen})
        with mock.patch.object(lookout, "subprocess", fake):
            popen.return_value.pid = 2 ** 22 + 12345            # not a live process

            def checks():
                return [c for c in popen.call_args_list if "check" in c.args[0]]
            self.tick.once()
            self.tick.once()
            self.assertEqual(len(checks()), 1)
            pidfile = lookout.lookout_dir(self.home) / "check.pid"
            self.assertEqual(pidfile.read_text(), str(2 ** 22 + 12345))
            os.utime(pidfile, (1, 1))                           # last launch long ago
            self.tick.once()
            self.assertEqual(len(checks()), 2)


if __name__ == "__main__":
    unittest.main()
