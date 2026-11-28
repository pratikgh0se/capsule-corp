"""M137 round 2: the review fixes (Z4 human-only overrides, growth and disk limits, livelocks,
the whole-run timeout, one ledger). The fakes and helpers are test_image_requests's `Case`."""
import os
import shutil
import time
import unittest
from argparse import Namespace
from unittest import mock

from capsule import blocks, db, image_build, image_ledger, spool
from capsule import image_request_run as run, image_requests as ir
from capsule.tests.test_image_requests import H2, NEW, NOW, SCHEMAS, TOML, Case


class Round2(Case):
    def worker_unblock(self, card):
        """A worker-role unblock through the real spool path, aimed at `card`."""
        tid, c, run_ = self.waiter()[0]
        sp = spool.make(self.tmp / "spool", c.run_id)
        doc = {"schema": "request.v1", "run_id": c.run_id, "task_id": tid, "verb": "unblock",
               "claim_lock": c.claim_lock, "target_task": card}
        os.utime(spool.write_doc(sp, "requests", doc), (time.time() - 60,) * 2)
        return spool.apply(self.conn, sp, run_, role_verbs={"unblock", "reassign"},
                           schemas=SCHEMAS, now=NOW)

    def test_z4_a_worker_unblock_of_an_image_card_is_refused_and_lifts_nothing(self):
        card = self.filed()
        for _ in range(2):
            image_ledger.append(self.home, "try", NOW, board="t", task=card, minutes=1)
        self.step(0)
        self.assertEqual(self.status(card), "blocked")
        self.assertEqual(self.worker_unblock(card), ["invalid"])
        self.assertEqual(self.status(card), "blocked")
        blocks.unblock(self.conn, card, by="worker", now=NOW)      # even if one slipped through
        self.step(1)
        self.assertEqual((image_ledger.rows(self.home, "override"), self.spawned), ([], []))
        self.assertIn(self.status(card), ("blocked", "triage"))

    def test_growth_over_1gb_with_a_human_override_succeeds(self):
        os.environ["FAKE_NEW_SIZE"] = str(2 * 10**9)
        self.addCleanup(os.environ.pop, "FAKE_NEW_SIZE", None)
        card = self.filed()
        self.step(0)
        self.step(1)
        self.assertEqual(self.status(card), "blocked")
        blocks.unblock(self.conn, card, by="human", now=NOW)
        self.step(2)
        self.step(3)
        self.assertEqual(self.status(card), "done")

    def test_an_override_never_waives_the_disk_floor(self):
        os.environ["FAKE_NEW_SIZE"] = str(2 * 10**9)
        self.addCleanup(os.environ.pop, "FAKE_NEW_SIZE", None)
        card = self.filed()
        self.step(0)
        self.step(1)
        blocks.unblock(self.conn, card, by="human", now=NOW)
        self.step(2)
        self.free = 10 * 10**9
        self.step(3)
        self.assertEqual(self.status(card), "blocked")
        self.assertIn("D10", self.reason(card))
        self.assertNotIn(NEW, (self.home / "images.lock").read_text())

    def test_first_request_with_no_recorded_image_is_not_blocked_for_growth(self):
        (self.home / "images.lock").write_text(f"capsule-python@sha256:{H2}\n")
        os.environ["FAKE_NEW_SIZE"] = str(5 * 10**9)
        self.addCleanup(os.environ.pop, "FAKE_NEW_SIZE", None)
        card = self.filed()
        self.step(0)
        self.step(1)
        self.assertEqual(self.status(card), "done")

    def test_one_cards_error_does_not_starve_the_next_card(self):
        self.ask(self.waiter()[0], tool="ripgrep", bin="rg")
        self.ask(self.waiter()[0], tool="fd", bin="fd")
        first, second = [c["id"] for c in self.cards()]
        real = ir._prepare

        def flaky(cfg, home, tid, f):
            if tid == first:
                raise RuntimeError("git clone: boom")
            return real(cfg, home, tid, f)
        with mock.patch.object(ir, "_prepare", flaky):
            self.step(0)
        self.assertEqual(self.spawned[0][0], second)
        events = self.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND "
                                   "kind='image_request_error'", (first,)).fetchall()
        self.assertEqual(len(events), 1)

    def test_tool_already_in_apt_blocks_with_a_clear_reason(self):
        self.make_repo(TOML.replace('smoke = ["python -V"]',
                                    'apt = ["ripgrep"]\nsmoke = ["python -V", "command -v rg"]'))
        card = self.filed()
        self.step()
        self.assertEqual((self.status(card), self.spawned), ("blocked", []))
        self.assertIn("already in [image].apt", self.reason(card))

    def test_a_permanent_git_error_blocks_after_three_passes_each_leaving_an_event(self):
        self.cfg["repo_url"] = str(self.tmp / "no-such-repo.git")
        card = self.filed()
        for n in range(3):
            self.step(n)
        self.assertEqual(self.status(card), "blocked")
        events = self.conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND "
                                   "kind='image_request_error'", (card,)).fetchone()[0]
        self.assertEqual(events, 3)
        self.assertEqual(self.spawned, [])

    def test_the_timeout_bounds_the_whole_run_not_only_the_build(self):
        os.environ["FAKE_SLEEP"] = "8"
        self.addCleanup(os.environ.pop, "FAKE_SLEEP", None)
        began = time.monotonic()
        digest, reason = image_build.build(self.tmp / "seed", "t", self.home, total_s=2)
        self.assertIsNone(digest)
        self.assertIn("smoke", reason)
        self.assertLess(time.monotonic() - began, 6)

    def test_filing_and_the_step_share_one_ledger_whatever_the_env_home_is(self):
        other = self.tmp / "other-home"
        with mock.patch.dict(os.environ, {"CAPSULE_HOME": str(other)}):
            self.filed()
        self.assertEqual(len(image_ledger.rows(self.home, "filed")), 1)
        self.assertFalse(other.exists())

    def test_a_result_written_just_before_the_lock_check_is_not_a_death(self):
        card = self.filed()
        self.dead = True
        self.step(0)                                    # started, nothing written yet
        real = ir._lock

        def finishing_builder(home):
            """The builder writes its result and exits right as the step looks at the lock."""
            if not (home / "image" / f"{card}.json").exists():
                run.run(Namespace(board="t", task=card, capsule_home=str(home), timeout=60))
            return real(home)
        with mock.patch.object(ir, "_lock", finishing_builder):
            self.step(1)
        self.assertEqual(self.status(card), "done")
        self.assertEqual(self.tries(card), 1)
        self.assertIn(f"capsule-proj-t@{NEW}", (self.home / "images.lock").read_text())

    def test_the_real_spawn_holds_the_lock_while_it_runs_and_frees_it_after(self):
        os.environ["FAKE_SLEEP"] = "3"
        self.addCleanup(os.environ.pop, "FAKE_SLEEP", None)
        work = self.home / "image" / "t_real" / "work"
        shutil.copytree(self.tmp / "seed", work)
        lock = ir._lock(self.home)
        mock.patch.stopall()                            # the real _spawn, not the test's fake
        mock.patch.dict(os.environ, {"AGENT_OPS_DOCKER_BIN": str(self.bin),
                                     "FAKE_LOG": str(self.log), "OPS_ENV_FILE": str(self.tmp / "n"),
                                     "CAPSULE_HOME": str(self.home), "FAKE_SLEEP": "3"}).start()
        with lock:
            ir._spawn("t", "t_real", self.home, 60, lock)
        busy = ir._lock(self.home)
        self.assertIsNone(busy)                         # the child holds it
        result = self.home / "image" / "t_real.json"
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and not result.exists():
            time.sleep(0.2)
        self.assertTrue(result.exists())
        free = ir._lock(self.home)
        while free is None and time.monotonic() < deadline:
            time.sleep(0.2)
            free = ir._lock(self.home)
        self.assertIsNotNone(free)                      # gone: the lock is free again
        free.close()

    def test_a_human_unblock_from_before_the_latest_hold_grants_no_override(self):
        card = self.filed()
        for _ in range(2):
            image_ledger.append(self.home, "try", NOW, board="t", task=card, minutes=1)
        db.event(self.conn, card, "unblocked", {"by": "human"}, now=NOW)     # while in flight
        blocks.block(self.conn, card, kind="needs_input", reason="D8 limit", now=NOW)
        self.conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (card,))   # not a human
        self.step(1)
        self.assertEqual((image_ledger.rows(self.home, "override"), self.spawned), ([], []))

    def test_an_override_is_consumed_by_its_own_unblock_event(self):
        card = self.filed()
        for _ in range(2):
            image_ledger.append(self.home, "try", NOW, board="t", task=card, minutes=1)
        self.step(0)
        blocks.unblock(self.conn, card, by="human", now=NOW)
        self.step(1)
        self.step(2)
        got = image_ledger.rows(self.home, "override")
        self.assertEqual(len(got), 1)
        self.assertIsInstance(got[0]["event"], int)

    def test_an_override_leaves_the_20gb_total_in_force(self):
        os.environ["FAKE_OLD_SIZE"], os.environ["FAKE_NEW_SIZE"] = str(21 * 10**9), str(23 * 10**9)
        self.addCleanup(os.environ.pop, "FAKE_OLD_SIZE", None)
        self.addCleanup(os.environ.pop, "FAKE_NEW_SIZE", None)
        card = self.filed()
        self.step(0)
        self.step(1)
        self.assertIn("D10", self.reason(card))
        blocks.unblock(self.conn, card, by="human", now=NOW)
        self.step(2)
        self.step(3)
        self.assertEqual(self.status(card), "blocked")
        self.assertIn("20 GB", self.reason(card))
        self.assertNotIn(NEW, (self.home / "images.lock").read_text())


if __name__ == "__main__":
    unittest.main()
