"""The weekly Security panels read denied_tools (crashed) and expires_at (override) from the shipped
event payload, so the Lookout must copy both; other event kinds must not carry them."""
import unittest

from capsule import db
from capsule.tests import test_lookout as tl   # module import: its cases are not re-collected


class SecurityPayload(tl.Board):
    def shipped(self, kind):
        return [e["payload"] for e in self.events() if e["payload"]["event"] == kind]

    def test_crashed_event_ships_denied_tools(self):
        tid = db.new_task(self.conn, title="t", branch="epic/screens")
        db.event(self.conn, tid, "crashed", {"error_class": "denied", "tool": "Bash",
                                             "denied_tools": ["Bash", "Write"]})
        self.collect()
        self.assertEqual(self.shipped("crashed")[0]["denied_tools"], ["Bash", "Write"])

    def test_override_event_ships_expires_at(self):
        tid = db.new_task(self.conn, title="t", branch="epic/screens")
        db.event(self.conn, tid, "override", {"check": "c", "reason": "r", "expires_at": 1.8e9})
        db.event(self.conn, tid, "blocked", {"reason": "x"})
        self.collect()
        self.assertEqual(self.shipped("override")[0]["expires_at"], 1.8e9)
        self.assertNotIn("expires_at", self.shipped("blocked")[0])
        self.assertNotIn("denied_tools", self.shipped("blocked")[0])


if __name__ == "__main__":
    unittest.main()
