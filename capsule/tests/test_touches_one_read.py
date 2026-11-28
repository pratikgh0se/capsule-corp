"""D251: touches reads host.json once per run, not once per decision row."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import touches


class OneRead(unittest.TestCase):
    def test_touches_reads_host_json_once_per_run(self):
        rows = [{"topic": "", "text": f"row {i}", "date": "2026-01-01", "number": i}
                for i in range(5)]
        with mock.patch.object(touches.decision_log, "find_log", return_value=Path(__file__)), \
                mock.patch.object(touches.decision_log, "parse", return_value=rows), \
                mock.patch.object(touches.host, "owner_name", return_value="Ada") as read:
            touches._owner_rows(tempfile.gettempdir(), 0)
        self.assertEqual(read.call_count, 1)


if __name__ == "__main__":
    unittest.main()
