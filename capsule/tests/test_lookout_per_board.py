# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
#   Intent.test_intent_a_board_tick_never_shares_a_spool_file,
#   PerBoardSpool.test_a_rotation_touches_only_its_own_boards_spool,
#   PerBoardSpool.test_the_legacy_tick_keeps_the_shared_spool
"""Off-legacy C05 (factory-autonomy Task 15): one Lookout spool per board, one shipper for all.
Oracle 1: the files on disk (literal paths, independent of the lookout.py helpers under test).
Oracle 2: the fake docker's captured scripts and each spool's own offset after a successful ship."""
import hashlib
import json
import os
import shutil
import fcntl
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import db, lookout, lookout_ship, paths
from capsule.tests.test_lookout import Board
from capsule.tests.test_lookout_ship import INTENT, ShipCase, ev, mark


def collector(home, board, pid, per_board=True):
    return lookout.Collector(home, board, host="h", pid=pid, per_board=per_board)


def run_status(case):
    import contextlib
    import io
    from capsule import lookout_cli
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        case.assertEqual(lookout_cli.main(["status", "--capsule-home", str(case.home)]), 0)
    return json.loads(out.getvalue())


class Intent(ShipCase):
    def test_intent_ship_covers_the_shared_and_every_board_spool(self):
        """Oracle 1: the fake docker saw one script per spool (the shared one plus the board's own).
        Oracle 2: each spool's own offset sits at that file's own size and each script carries only
        its own spool's rows (the shared script e1 not e2, the board's e2 not e1)."""
        self.write([ev(1), mark(1)])
        rows = [ev(2, "capsule-corp"), mark(2, "capsule-corp")]
        collector(self.home, "capsule-corp", 2)._append(rows)
        self.assertEqual(lookout_ship.ship(self.home), 0)
        scripts = self.scripts()
        self.assertEqual(len(scripts), 2)
        ids = [{json.loads(r[2])["event_id"] for r in self.staged(s) if r[1] == "events"}
               for s in scripts]
        self.assertEqual(ids, [{"e1"}, {"e2"}])
        for sub in ("", "capsule-corp/"):
            d = self.home / "lookout" / sub
            size = (d / "spool.jsonl").stat().st_size
            self.assertEqual(int((d / "spool.offset").read_text()), size)
            self.assertTrue((d / "ship.last.json").is_file())
        self.assertEqual(lookout_ship.status(self.home, "capsule-corp")["last"]["rc"], 0)

    def test_intent_a_board_spool_is_never_rewritten_while_its_shipper_holds_the_lock(self):
        """Oracle 1: with lookout/<board>/ship.lock held, maintain_spool for that board returns busy
        and the spool is byte-identical. Oracle 2: once the lock is released the same call rotates
        it (so the first result was the lock, not a spool that never needed work)."""
        collector(self.home, "capsule-corp", 2)._append([ev(2, "capsule-corp")])
        old = lookout.ROTATE_BYTES
        lookout.ROTATE_BYTES = 0
        self.addCleanup(setattr, lookout, "ROTATE_BYTES", old)
        sp = self.home / "lookout" / "capsule-corp" / "spool.jsonl"
        (sp.parent / "spool.offset").write_text("999999")
        before = sp.read_bytes()
        with open(sp.parent / "ship.lock", "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(lookout.maintain_spool(self.home, "capsule-corp"), "busy")
            self.assertEqual(sp.read_bytes(), before)
        self.assertEqual(lookout.maintain_spool(self.home, "capsule-corp"), "rotated")

    def test_intent_a_board_tick_never_shares_a_spool_file(self):
        """Oracle 1: two board collectors leave two spools, each holding only its own line.
        Oracle 2: the shared spool does not exist."""
        for board, pid in (("my-app", 1), ("capsule-corp", 2)):
            collector(self.home, board, pid)._append([ev(pid, board)])
        for board in ("my-app", "capsule-corp"):
            lines = (self.home / "lookout" / board / "spool.jsonl").read_text().splitlines()
            self.assertEqual([json.loads(x)["row"]["board"] for x in lines], [board])
        self.assertFalse((self.home / "lookout" / "spool.jsonl").exists())

    def test_intent_status_reports_every_spool_and_a_total(self):
        self.write([ev(1)])
        collector(self.home, "capsule-corp", 2)._append([ev(2, "capsule-corp")])
        collector(self.home, "other", 3)._append([ev(3, "other"), ev(4, "other")])
        sizes = {b: (self.home / "lookout" / b / "spool.jsonl").stat().st_size
                 for b in ("capsule-corp", "other")}
        shared = (self.home / "lookout" / "spool.jsonl").stat().st_size
        got = run_status(self)
        self.assertEqual(got["spool_bytes"], shared)
        self.assertEqual(got["unshipped_bytes"], shared)
        self.assertEqual(sorted(got["boards"]), ["capsule-corp", "other"])
        for b, n in sizes.items():
            self.assertEqual(got["boards"][b]["unshipped_bytes"], n)
            self.assertEqual(got["boards"][b]["offset"], 0)
        self.assertEqual(got["total_unshipped_bytes"], shared + sum(sizes.values()))

    def test_file_matches_tests(self):
        text = (INTENT / "per_board.jsonl").read_text()
        sum_lines = (INTENT / "SHA256SUMS").read_text().splitlines()
        sums = dict(reversed(l.split(maxsplit=1)) for l in sum_lines)
        self.assertEqual(sums["per_board.jsonl"], hashlib.sha256(text.encode()).hexdigest())
        ids = {json.loads(l)["id"].replace("-", "_") for l in text.splitlines()}
        tests = {n[len("test_intent_"):] for n in dir(Intent) if n.startswith("test_intent_")}
        self.assertEqual(ids, tests)


class PerBoardSpool(ShipCase):
    def test_the_legacy_tick_keeps_the_shared_spool(self):
        collector(self.home, "my-app", 1, per_board=False)._append([ev(1)])
        self.assertTrue((self.home / "lookout" / "spool.jsonl").is_file())
        self.assertFalse((self.home / "lookout" / "my-app").exists())

    def test_status_reads_each_boards_own_spool(self):
        self.write([ev(1)])
        rows = [ev(2, "capsule-corp"), ev(3, "capsule-corp")]
        collector(self.home, "capsule-corp", 2)._append(rows)
        own = (self.home / "lookout" / "capsule-corp" / "spool.jsonl").stat().st_size
        self.assertEqual(lookout_ship.status(self.home, "capsule-corp")["spool_bytes"], own)
        self.assertEqual(lookout_ship.status(self.home)["spool_bytes"],
                         (self.home / "lookout" / "spool.jsonl").stat().st_size)

    def test_a_rotation_touches_only_its_own_boards_spool(self):
        collector(self.home, "capsule-corp", 2)._append([ev(2, "capsule-corp")])
        collector(self.home, "my-app", 1)._append([ev(1)])
        old = lookout.ROTATE_BYTES
        lookout.ROTATE_BYTES = 0
        self.addCleanup(setattr, lookout, "ROTATE_BYTES", old)
        size = lookout.spool_path(self.home, "my-app").stat().st_size
        lookout.offset_path(self.home, "capsule-corp").write_text("999999")
        lookout.offset_path(self.home, "my-app").write_text(str(size))
        self.assertEqual(lookout.maintain_spool(self.home, "capsule-corp"), "rotated")
        self.assertEqual(lookout.spool_path(self.home, "my-app").stat().st_size, size)

    def test_a_rejected_row_lands_beside_its_own_boards_spool(self):
        rows = [ev(i, "capsule-corp") for i in (1, 2, 3)]
        collector(self.home, "capsule-corp", 2)._append(rows)
        os.environ["FAKE_PSQL_REJECT_IF_CONTAINS"] = "e2"
        self.assertEqual(lookout_ship.ship(self.home), 0)
        rej = (self.home / "lookout" / "capsule-corp" / "spool.rejected").read_text().splitlines()
        self.assertEqual(len(rej), 1)
        self.assertFalse((self.home / "lookout" / "spool.rejected").exists())

    def test_the_rejected_sidecar_rotates_per_board(self):
        old = lookout.REJECTED_MAX_BYTES
        lookout.REJECTED_MAX_BYTES = 3000
        self.addCleanup(setattr, lookout, "REJECTED_MAX_BYTES", old)
        rows = [ev(i, "capsule-corp") for i in range(1, 12)]
        collector(self.home, "capsule-corp", 2)._append(rows)
        os.environ["FAKE_PSQL_REJECT_IF_CONTAINS"] = "|".join(f"e{i}" for i in range(2, 12))
        self.assertEqual(lookout_ship.ship(self.home), 0)
        d = self.home / "lookout"
        self.assertTrue((d / "capsule-corp" / "spool.rejected.1").is_file())
        self.assertFalse((d / "spool.rejected.1").exists())

    def test_a_boards_recent_prices_stamp_comes_from_its_own_ship_last(self):
        import time
        collector(self.home, "capsule-corp", 2)._append([ev(1, "capsule-corp")])
        (self.home / "lookout" / "capsule-corp" / "ship.last.json").write_text(
            json.dumps({"prices_at": time.time(), "rc": 0}))
        self.assertEqual(lookout_ship.ship(self.home), 0)
        self.assertNotIn("usd_in_per_mtok", self.scripts()[0])

    def test_a_losing_shipper_touches_neither_the_pid_nor_the_events_spool(self):
        root = self.home / "lookout"
        (root / "ship.pid").write_text("4242")
        self.write([ev(1)])
        with open(root / "ship.run.lock", "a") as held, \
                mock.patch.object(lookout_ship.events, "ship") as events_ship:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(lookout_ship.ship(self.home), 0)
            seen = ((root / "ship.pid").read_text(), events_ship.call_count, self.scripts())
            self.assertEqual(seen, ("4242", 0, []))
        with mock.patch.object(lookout_ship.events, "ship") as events_ship:
            self.assertEqual(lookout_ship.ship(self.home), 0)
        seen = ((root / "ship.pid").read_text(), events_ship.call_count, len(self.scripts()))
        self.assertEqual(seen, (str(os.getpid()), 1, 1))

    def test_check_counts_unshipped_bytes_of_every_board_spool(self):
        collector(self.home, "capsule-corp", 2)._append([ev(1, "capsule-corp")])
        self.assertGreater(lookout_ship.unshipped(self.home), 0)


class PerBoardCollect(Board):
    def setUp(self):
        super().setUp()
        self.col = lookout.Collector(self.home, "my-app", host="h", pid=1, per_board=True)
        self.own = self.home / "lookout" / "my-app"

    def test_the_tickmark_counts_the_boards_own_unshipped_bytes(self):
        self.collect()
        size = (self.own / "spool.jsonl").stat().st_size
        (self.own / "spool.offset").write_text("10")
        (self.home / "lookout" / "spool.offset").write_text(str(size))   # decoy: shared offset
        self.collect()
        marks = [json.loads(x) for x in (self.own / "spool.jsonl").read_text().splitlines()
                 if '"tickmark"' in x]
        self.assertEqual(marks[-1]["row"]["spool_bytes_unshipped"], size - 10)

    def test_collect_rotates_the_boards_own_shipped_spool(self):
        self.collect()
        old = lookout.ROTATE_BYTES
        lookout.ROTATE_BYTES = 0
        self.addCleanup(setattr, lookout, "ROTATE_BYTES", old)
        (self.own / "spool.offset").write_text("999999999")
        self.collect()
        seen = ((self.own / "spool.jsonl").stat().st_size, (self.own / "spool.offset").read_text())
        self.assertEqual(seen, (0, "0"))
        self.assertFalse((self.home / "lookout" / "spool.jsonl").exists())


class TickBuildsThePerBoardCollector(unittest.TestCase):
    """A real `--board` Tick pass, observed on disk: its tickmark lands in `lookout/<board>/` and
    the shared spool is never created."""

    def test_a_board_tick_writes_its_own_spool(self):
        from capsule import tick
        from capsule.tests.test_tick import FakeEvents, FakeRunner
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, True)
        d = paths.board_dir("capsule-corp", home)
        d.mkdir(parents=True)
        base = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        (d / "config.json").write_text(json.dumps(dict(base, board="capsule-corp")))
        db.create_board(d / "kanban.db")
        runner = FakeRunner()
        runner.up = False
        t = tick.Tick(home, board="capsule-corp", runners=lambda tier, role: runner,
                      events=FakeEvents(), alive=lambda pid: False)
        counts = t.once()
        self.assertEqual((counts["config_error"], counts["lookout_errors"]), (0, 0))
        own = home / "lookout" / "capsule-corp" / "spool.jsonl"
        self.assertIn("tickmark", [json.loads(x)["t"] for x in own.read_text().splitlines()])
        self.assertFalse((home / "lookout" / "spool.jsonl").exists())


class StatusCli(ShipCase):
    """`lookout status` reports every spool. Oracle 1: literal byte counts from the files written
    here (len of the lines). Oracle 2: the shared spool's flat fields still match status(home)."""

    def test_status_total_drops_to_zero_once_every_spool_has_shipped(self):
        self.write([ev(1), mark(1)])
        rows = [ev(2, "capsule-corp"), mark(2, "capsule-corp")]
        collector(self.home, "capsule-corp", 2)._append(rows)
        self.assertGreater(run_status(self)["total_unshipped_bytes"], 0)
        self.assertEqual(lookout_ship.ship(self.home), 0)
        got = run_status(self)
        self.assertEqual(got["total_unshipped_bytes"], 0)
        self.assertEqual(got["boards"]["capsule-corp"]["last"]["rc"], 0)

    def test_status_with_no_board_spools_has_an_empty_boards_map(self):
        got = run_status(self)
        self.assertEqual((got["boards"], got["total_unshipped_bytes"]), ({}, 0))


if __name__ == "__main__":
    unittest.main()
