"""Loophole triage 2026-10-27, korin rows 1, 2 and 5: the literal intent rows ban one wording of a
free-text field, and rewording dodges them. Korin's gate now backs them with code: an `enforcer`
must name a mechanism, a `root_cause` must not restate the symptom. Z2: each test fails on the
code before these checks."""
import unittest

from capsule import incidents
from capsule.tests.test_incidents import Base, good_doc

ALERT_KEY = "alert unowned-idle fired twice today"


class WeakEnforcer(unittest.TestCase):
    def test_free_text_enforcers_are_weak(self):
        for text in ("code review by the CoS", "Review", "review", "a line in each brief", "",
                     "code review by the CoS, see t_0123abcd",
                     "a line in docs/BRIEF.md",
                     "enforced by review until mechanised: code review by the CoS",
                     "enforced by review until mechanised: later"):
            self.assertTrue(incidents._weak_enforcer(text), text)

    def test_named_mechanisms_pass(self):
        for text in ("capsule/worker.py run_one finally-block",
                     "capsule.tests.test_x via test_x_works",
                     "enforced by review until mechanised: the fix card for this incident",
                     "Enforced by review until mechanized: t_0123abcd",
                     "enforced by review until mechanised: tests/test_x.sh::label"):
            self.assertFalse(incidents._weak_enforcer(text), text)


class PostmortemRefusals(Base):
    def _problems(self, incident_id, **over):
        return incidents.postmortem_problems(good_doc(incident_id, **over),
                                             incidents.get(self.conn, incident_id))

    def test_reworded_review_enforcer_is_refused(self):
        a, _ = self.open()
        for text in ("code review by the CoS", "Review", "a line in each brief"):
            self.assertTrue(any("enforcer" in p for p in self._problems(a, enforcer=text)), text)

    def test_a_good_document_passes(self):
        a, _ = self.open()
        self.assertEqual(self._problems(a), [])

    def test_reworded_symptom_root_cause_is_refused_on_a_silent_failure(self):
        a, _ = self.open(kind="silent_failure", key=ALERT_KEY)
        for text in ("The alert fired two times in one day.", "the alert fired twice",
                     "Alert fires again."):
            problems = self._problems(a, root_cause=text)
            self.assertTrue(any("root_cause" in p for p in problems), text)

    def test_a_root_cause_that_only_repeats_the_incident_key_is_refused(self):
        a, _ = self.open(key="implementer: fatal: adapter raised empty reply")
        problems = self._problems(a, root_cause="Adapter raised an empty reply.")
        self.assertTrue(any("root_cause" in p for p in problems))

    def test_a_cause_that_explains_the_symptom_passes(self):
        a, _ = self.open(kind="silent_failure", key=ALERT_KEY)
        cause = "The alert fired again because the idle detector never cleared its latch."
        self.assertEqual(self._problems(a, root_cause=cause), [])


if __name__ == "__main__":
    unittest.main()
