"""Expiry on `claude -r` resume pointers (row 128, M085). A transcript dies at
~30 days, so a handle with no expiry is a pointer that looks valid and is not."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import resume_pointers as rp


class Scan(unittest.TestCase):
    def _vault(self, td, files):
        v = Path(td)
        for rel, text in files.items():
            p = v / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        return v

    def test_a_handle_with_no_expiry_is_reported_as_missing(self):
        with tempfile.TemporaryDirectory() as td:
            v = self._vault(td, {"a.md": "resume: `claude -r session_01ABC`\n"})
            found = rp.scan(v, today="2026-09-10")
            self.assertEqual(found[0]["state"], "no-expiry")
            self.assertEqual(found[0]["session"], "session_01ABC")

    def test_an_expiry_in_the_future_is_live(self):
        with tempfile.TemporaryDirectory() as td:
            v = self._vault(td, {"a.md": "`claude -r session_01ABC` (expires: 2026-10-01)\n"})
            self.assertEqual(rp.scan(v, today="2026-09-10")[0]["state"], "live")

    def test_an_expiry_in_the_past_is_expired(self):
        with tempfile.TemporaryDirectory() as td:
            v = self._vault(td, {"a.md": "`claude -r session_01ABC` (expires: 2026-09-01)\n"})
            self.assertEqual(rp.scan(v, today="2026-09-10")[0]["state"], "expired")

    def test_the_default_expiry_is_thirty_days_from_the_notes_date(self):
        self.assertEqual(rp.default_expiry("2026-09-10"), "2026-10-10")

    def test_a_note_with_no_handle_is_not_reported(self):
        with tempfile.TemporaryDirectory() as td:
            v = self._vault(td, {"a.md": "no handles here\n"})
            self.assertEqual(rp.scan(v, today="2026-09-10"), [])

    def test_the_scan_skips_machine_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            v = self._vault(td, {"copilot/a.md": "`claude -r session_01X`\n"})
            self.assertEqual(rp.scan(v, today="2026-09-10"), [])
