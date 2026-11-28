import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("AGENT_OPS_NO_SEND", "1")

import messages  # noqa: E402

D1 = datetime.datetime(2026, 9, 11, 8, 30).astimezone()
D2 = datetime.datetime(2026, 9, 12, 8, 30).astimezone()
D3 = datetime.datetime(2026, 9, 13, 8, 30).astimezone()


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="msgbudget-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = self.dir / "message-budget.json"
        self.batch = self.dir / "brief-batch.json"
        self._flag = os.environ.get("OPS_BRIEF_V2")
        os.environ["OPS_BRIEF_V2"] = "1"
        self._actionable_max = os.environ.get("OPS_ACTIONABLE_MAX")
        os.environ.pop("OPS_ACTIONABLE_MAX", None)

    def tearDown(self):
        if self._flag is None:
            os.environ.pop("OPS_BRIEF_V2", None)
        else:
            os.environ["OPS_BRIEF_V2"] = self._flag
        if self._actionable_max is None:
            os.environ.pop("OPS_ACTIONABLE_MAX", None)
        else:
            os.environ["OPS_ACTIONABLE_MAX"] = self._actionable_max


class Flag(Base):
    def test_off_by_default(self):
        os.environ["OPS_BRIEF_V2"] = "0"
        self.assertFalse(messages.enabled())
        os.environ.pop("OPS_BRIEF_V2")
        self.assertFalse(messages.enabled())

    def test_on_reads_only_the_exact_string_1(self):
        os.environ["OPS_BRIEF_V2"] = "1"
        self.assertTrue(messages.enabled())

    def test_i2_other_spellings_read_as_off(self):
        # I-2: run.sh/report.sh's own gates test `= "1"` exactly. Before this
        # fix, messages.enabled() accepted "true"/"yes"/"on" case-insensitively
        # too, so OPS_BRIEF_V2=true turned messages.py fully ON while
        # report.sh composed neither the brief nor the close - deferred items
        # with no morning brief to surface them. Narrowing to the exact
        # string "1" makes the two spellings one flag.
        for v in ("true", "TRUE", "True", "yes", "YES", "on", "ON", "01", "1.0"):
            os.environ["OPS_BRIEF_V2"] = v
            self.assertFalse(messages.enabled(), v)


class Counting(Base):
    def test_a_fresh_day_starts_at_zero(self):
        c = messages.counts(now=D1, path=self.path)
        self.assertEqual((c["day"], c["scheduled"], c["actionable"], c["page"], c["deferred"]),
                         (D1.date().isoformat(), 0, 0, 0, 0))

    def test_record_increments_and_persists(self):
        messages.record("scheduled", now=D1, path=self.path)
        messages.record("actionable", now=D1, path=self.path)
        messages.record("actionable", now=D1, path=self.path)
        c = messages.counts(now=D1, path=self.path)
        self.assertEqual((c["scheduled"], c["actionable"]), (1, 2))
        self.assertEqual(json.loads(self.path.read_text())["day"], D1.date().isoformat())

    def test_a_new_day_resets_and_carries_yesterdays_deferrals(self):
        messages.record("actionable", now=D1, path=self.path)
        messages.defer_actionable("DECISION x", now=D1, path=self.path, batch_path=self.batch)
        c = messages.counts(now=D2, path=self.path)
        self.assertEqual((c["actionable"], c["deferred"], c["carry"]), (0, 0, 1))


class Allow(Base):
    def test_the_default_caps_make_ten_a_day(self):
        # D209 2026-11-11: 10 messages a day total = 2 scheduled + 8 actionable
        self.assertEqual(messages._limit("scheduled"), 2)
        self.assertEqual(messages._limit("actionable"), 8)
        self.assertEqual(messages._limit("scheduled") + messages._limit("actionable"), 10)

    def test_the_ninth_actionable_is_refused(self):
        for _ in range(8):
            ok, _why = messages.allow("actionable", now=D1, path=self.path)
            self.assertTrue(ok)
            messages.record("actionable", now=D1, path=self.path)
        ok, why = messages.allow("actionable", now=D1, path=self.path)
        self.assertFalse(ok)
        self.assertEqual(why, "8/8 actionable sent today")

    def test_a_page_is_never_withheld(self):
        for _ in range(5):
            messages.record("page", now=D1, path=self.path)
        ok, why = messages.allow("page", now=D1, path=self.path)
        self.assertTrue(ok)
        self.assertEqual(why, "pages are never withheld")

    def test_the_cap_is_overridable(self):
        os.environ["OPS_ACTIONABLE_MAX"] = "1"
        messages.record("actionable", now=D1, path=self.path)
        self.assertFalse(messages.allow("actionable", now=D1, path=self.path)[0])

    def test_a_corrupt_file_is_permissive(self):
        self.path.write_text("{ not json")
        self.assertTrue(messages.allow("actionable", now=D1, path=self.path)[0])
        self.assertIsNone(messages.budget_line(now=D1, path=self.path))

    def test_an_unwritable_path_never_raises(self):
        self.assertEqual(messages.record("actionable", path="/proc/nope/x.json")["actionable"], 1)
        self.assertEqual(messages.defer_actionable("x", path="/proc/nope/x.json",
                                                   batch_path=self.batch), (1, True))


class Overflow(Base):
    def test_a_deferral_lands_in_c2s_batch_per_call(self):
        import budgets
        messages.defer_actionable("DECISION - vector store", board="capsule-corp",
                                  task_id="t_9", now=D1, path=self.path,
                                  batch_path=self.batch)
        messages.defer_actionable("DECISION - cache ttl", board="capsule-corp",
                                  task_id="t_10", now=D1, path=self.path,
                                  batch_path=self.batch)
        rows = json.loads(self.batch.read_text())
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["kind"] for r in rows], ["waiting", "waiting"])
        self.assertEqual(budgets.brief_lines(path=self.batch),
                         ["waiting: capsule-corp/t_9 - DECISION - vector store",
                          "waiting: capsule-corp/t_10 - DECISION - cache ttl"])
        self.assertEqual(messages.counts(now=D1, path=self.path)["deferred"], 2)

    def test_budget_line_absent_until_something_is_deferred(self):
        messages.record("actionable", now=D1, path=self.path)
        self.assertIsNone(messages.budget_line(now=D1, path=self.path))
        messages.defer_actionable("DECISION y", now=D1, path=self.path, batch_path=self.batch)
        self.assertEqual(
            messages.budget_line(now=D1, path=self.path),
            "budget: 1 actionable item(s) waiting - the cap is 8/day (re-fit 2026-09-24)")

    def test_yesterdays_deferral_is_still_reported_this_morning(self):
        messages.defer_actionable("DECISION z", now=D1, path=self.path, batch_path=self.batch)
        self.assertIn("1 actionable item(s) waiting",
                      messages.budget_line(now=D2, path=self.path))

    def test_carry_is_a_one_day_window_and_then_clears(self):
        # CQ-4 (controller ruling): `carry` reports yesterday's deferrals
        # only. It must not itself roll forward a second time, or a
        # deferral would be reported as "waiting" forever.
        messages.defer_actionable("DECISION w", now=D1, path=self.path, batch_path=self.batch)
        c2 = messages.counts(now=D2, path=self.path)
        self.assertEqual(c2["carry"], 1)
        messages.record("scheduled", now=D2, path=self.path)  # touches day 2, no deferral
        c3 = messages.counts(now=D3, path=self.path)
        self.assertEqual(c3["carry"], 0)
        self.assertIsNone(messages.budget_line(now=D3, path=self.path))


class DefaultStateDirRefusal(unittest.TestCase):
    """Controller ruling 2 (refined per the report-brief follow-up, CQ-1): the
    default state path refuses to be WRITTEN under AGENT_OPS_NO_SEND unless
    an explicit path= or OPS_MESSAGE_STATE override is given - mirrors
    verdicts.py:verdict_dir(). Reads never raise: a block that cannot be
    computed is absent, never a broken job (the brief's fail-open rule) -
    an unoverridden default under test just reads as the blank/zero day.

    CQ-1: this class must never touch the REAL `maintenance/state` tree
    (that assertion would start failing the moment a real run writes
    `message-budget.json` there, and it would violate the "no test touches
    maintenance/state" constraint outright). So both `_DEFAULT_COUNTS` and
    `COUNTS` are rebound to the SAME scratch path for the duration of each
    test - `_explicit_path()` still sees them as equal (`COUNTS ==
    _DEFAULT_COUNTS`) and so still returns None, exercising the exact same
    "frozen default" branch of the guard, just against a throwaway file."""

    def setUp(self):
        self._no_send = os.environ.get("AGENT_OPS_NO_SEND")
        self._override = os.environ.get("OPS_MESSAGE_STATE")
        os.environ["AGENT_OPS_NO_SEND"] = "1"
        os.environ.pop("OPS_MESSAGE_STATE", None)
        self._saved_default = messages._DEFAULT_COUNTS
        self._saved_counts = messages.COUNTS
        self._dir = Path(tempfile.mkdtemp(prefix="msgbudget-defaultdir-"))
        self.addCleanup(shutil.rmtree, self._dir, ignore_errors=True)
        scratch_default = self._dir / "message-budget.json"
        messages._DEFAULT_COUNTS = scratch_default
        messages.COUNTS = scratch_default
        self.scratch_default = scratch_default

    def tearDown(self):
        if self._no_send is None:
            os.environ.pop("AGENT_OPS_NO_SEND", None)
        else:
            os.environ["AGENT_OPS_NO_SEND"] = self._no_send
        if self._override is None:
            os.environ.pop("OPS_MESSAGE_STATE", None)
        else:
            os.environ["OPS_MESSAGE_STATE"] = self._override
        messages._DEFAULT_COUNTS = self._saved_default
        messages.COUNTS = self._saved_counts

    def test_a_write_to_the_default_state_dir_is_refused_under_test(self):
        with self.assertRaises(RuntimeError):
            messages.record("actionable", now=D1)

    def test_an_explicit_path_bypasses_the_write_refusal(self):
        d = Path(tempfile.mkdtemp(prefix="msgbudget-guard-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        p = d / "message-budget.json"
        c = messages.record("actionable", now=D1, path=p)
        self.assertEqual(c["actionable"], 1)
        self.assertTrue(p.exists())
        self.assertEqual(json.loads(p.read_text())["actionable"], 1)

    def test_reads_never_raise_on_the_unoverridden_default_under_test(self):
        self.assertFalse(self.scratch_default.exists())
        c = messages.counts(now=D1)
        self.assertEqual((c["day"], c["actionable"], c["deferred"]),
                         (D1.date().isoformat(), 0, 0))
        self.assertIsNone(messages.budget_line(now=D1))
        ok, _why = messages.allow("actionable", now=D1)
        self.assertTrue(ok)
        self.assertFalse(self.scratch_default.exists())


class Dedupe(Base):
    """T8-C1: a card still over budget on the next ~10-minute sprint-sync
    tick must not re-append to C2's shared batch or re-bump `deferred` -
    both would turn one stuck card into ~144 identical rows/day."""

    def test_the_same_item_deferred_twice_writes_one_batch_row(self):
        import budgets
        first, new1 = messages.defer_actionable("Stuck", board="b", task_id="t",
                                                 now=D1, path=self.path, batch_path=self.batch)
        second, new2 = messages.defer_actionable("Stuck", board="b", task_id="t",
                                                  now=D1, path=self.path, batch_path=self.batch)
        self.assertEqual((first, new1), (1, True))
        self.assertEqual((second, new2), (1, False))
        self.assertEqual(budgets.brief_lines(path=self.batch), ["waiting: b/t - Stuck"])
        self.assertEqual(messages.counts(now=D1, path=self.path)["deferred"], 1)

    def test_two_different_task_ids_write_two_rows(self):
        import budgets
        messages.defer_actionable("Stuck one", board="b", task_id="t1",
                                  now=D1, path=self.path, batch_path=self.batch)
        messages.defer_actionable("Stuck two", board="b", task_id="t2",
                                  now=D1, path=self.path, batch_path=self.batch)
        self.assertEqual(len(budgets.brief_lines(path=self.batch)), 2)
        self.assertEqual(messages.counts(now=D1, path=self.path)["deferred"], 2)

    def test_a_new_day_clears_the_dedupe_record(self):
        messages.defer_actionable("Stuck", board="b", task_id="t",
                                  now=D1, path=self.path, batch_path=self.batch)
        _, is_new = messages.defer_actionable("Stuck", board="b", task_id="t",
                                              now=D2, path=self.path, batch_path=self.batch)
        self.assertTrue(is_new)
        self.assertEqual(messages.counts(now=D2, path=self.path)["deferred"], 1)

    def test_no_board_or_task_id_falls_back_to_a_text_hash(self):
        first, new1 = messages.defer_actionable("Some text", now=D1, path=self.path,
                                                 batch_path=self.batch)
        second, new2 = messages.defer_actionable("Some text", now=D1, path=self.path,
                                                  batch_path=self.batch)
        third, new3 = messages.defer_actionable("Other text", now=D1, path=self.path,
                                                 batch_path=self.batch)
        self.assertEqual((first, new1), (1, True))
        self.assertEqual((second, new2), (1, False))
        self.assertEqual((third, new3), (2, True))

class PrintedCapEqualsEnforcedCap(Base):
    """The brief child prints the cap `allow` enforces: report.sh exports the variable."""

    def _printed(self, ops_env):
        """The cap in the budget line, from a child that got only what report.sh exports."""
        sh = (Path(messages.__file__).parent / "report.sh").read_text(encoding="utf-8")
        export = next(x for x in sh.splitlines()
                      if not x.lstrip().startswith("#") and "export OPS_ACTIONABLE_MAX" in x)
        code = ("import messages as m, sys; p, b = sys.argv[1:3]; "
                "m.defer_actionable('x', path=p, batch_path=b); print(m.budget_line(path=p))")
        env = {k: v for k, v in os.environ.items() if k != "OPS_ACTIONABLE_MAX"}
        env["PYTHONPATH"] = str(Path(messages.__file__).parent)
        # like sourcing ops.env: a shell variable, unexported until report.sh's own line runs
        setv = "".join(f"{k}={v}\n" for k, v in ops_env.items())
        cmd = ["bash", "-c", setv + export + '\npython3 -c "$0" "$1" "$2"', code,
               str(self.path), str(self.batch)]
        out = subprocess.run(cmd, env=env, capture_output=True, text=True, check=True).stdout
        return int(re.search(r"cap is (\d+)/day", out).group(1))

    def _enforced(self, ops_env):
        """How many actionable sends `allow` lets through with the bridge's environment."""
        os.environ.update(ops_env)
        sent = 0
        while messages.allow("actionable", path=self.dir / "bridge.json")[0] and sent < 50:
            messages.record("actionable", path=self.dir / "bridge.json")
            sent += 1
        return sent

    def test_set(self):
        env = {"OPS_ACTIONABLE_MAX": "5"}
        self.assertEqual((self._printed(env), self._enforced(env)), (5, 5))

    def test_unset(self):
        self.assertEqual((self._printed({}), self._enforced({})), (messages.ACTIONABLE_MAX,) * 2)


if __name__ == "__main__":
    unittest.main()
