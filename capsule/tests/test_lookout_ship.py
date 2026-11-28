# mutation-check: targets-predate-range b957739c9 -- rename only, no behaviour pinned ::
#   Check.test_equal_counts_exit_0_and_the_a3_count_is_printed
"""Task 3 (The Lookout): the shipper contract, intent examples I1-I6, I8, I10, I11 (spec 11.1).
Oracle 1: a fake `docker` that captures the psql script (transport and contract).
Oracle 2: tests/test_migration_pg.sh ships the same script into a real postgres:16-alpine
(SQL semantics: dedupe, the snap_ts guard, drain order)."""
import csv
import fcntl
import hashlib
import http.server
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from capsule import db, lookout, lookout_ship, paths
from capsule.tests import tmpclean

FAKEBIN = Path(__file__).resolve().parent / "fixtures" / "fakebin"
INTENT = paths.INTENT / "lookout"
TOKEN = "ghp_" + "b" * 36


def ev(i, board="my-app"):
    return {"v": 1, "t": "event", "row": {"v": 1, "event_id": f"e{i}", "ts": "2026-09-27T05:00:00+00:00",
                                          "kind": "card", "source": "capsule", "board": board, "task_id": "t_1",
                                          "slug": None, "title": "card", "text": None, "link": None,
                                          "severity": "info", "payload": {"event": "claimed"}}}


def mark(n, board="my-app"):
    return {"v": 1, "t": "tickmark", "board": board, "snap_ts": f"2026-09-27T05:{n // 60:02d}:{n % 60:02d}+00:00",
            "pass_id": f"h:1:{n}", "row": {"host": "h", "pid": 1, "harness_sha": "abc", "last_pass_at":
            f"2026-09-27T05:{n // 60:02d}:{n % 60:02d}+00:00", "pass_ms": 10, "lookout_ms": 1, "counts": {},
            "running": 0, "ready": 0, "blocked": 0, "paused_accounts": [], "spool_bytes_unshipped": 0}}


def card(title="card", reason=None, ts="2026-09-27T05:00:00+00:00"):
    return {"v": 1, "t": "snap", "table": "cap_cards", "board": "my-app", "snap_ts": ts,
            "pass_id": "h:1:1",
            "row": {"task_id": "t_1", "title": title, "status": "blocked", "product": "my-app",
                    "phase": "waiting", "block_reason": reason}}


class ShipCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.cap = self.home / "captured.sql"
        self.env = {k: os.environ.get(k) for k in ("AGENT_OPS_DOCKER_BIN", "FAKE_PSQL_CAPTURE", "FAKE_PSQL_RC",
                                                    "FAKE_PSQL_SLEEP", "FAKE_PSQL_STDOUT_FILE", "FAKE_DOCKER_ARGV",
                                                    "FAKE_PSQL_REJECT_IF_CONTAINS", "FAKE_PSQL_REJECT_SQLSTATE",
                                                    "FAKE_PSQL_FAIL_ON_CALL", "FAKE_PSQL_FAIL_RC")}
        os.environ.update(AGENT_OPS_DOCKER_BIN=str(FAKEBIN), FAKE_PSQL_CAPTURE=str(self.cap), FAKE_PSQL_RC="0",
                          FAKE_PSQL_SLEEP="0")
        os.environ.pop("FAKE_PSQL_STDOUT_FILE", None)
        os.environ.pop("FAKE_DOCKER_ARGV", None)
        os.environ.pop("FAKE_PSQL_REJECT_IF_CONTAINS", None)
        os.environ.pop("FAKE_PSQL_REJECT_SQLSTATE", None)
        os.environ.pop("FAKE_PSQL_FAIL_ON_CALL", None)
        os.environ.pop("FAKE_PSQL_FAIL_RC", None)
        lookout.lookout_dir(self.home).mkdir(parents=True)

    def tearDown(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        tmpclean.sweep(self.home)

    def write(self, lines, raw_tail=""):
        with open(lookout.spool_path(self.home), "a", encoding="utf-8") as f:
            f.write("".join(json.dumps(l, ensure_ascii=False) + "\n" for l in lines) + raw_tail)

    def rejected_exists(self):
        """Spelled out (not `lookout.rejected_path`) so a check for the ABSENCE of a quarantine
        never depends on that helper existing - the two are independent, and a P1/P2-shaped test
        (whose own `ship()` assertion happens to match un-isolated behaviour too) must fail on
        its own logic rather than crash on an unrelated symbol lookup first."""
        return (self.home / "lookout" / "spool.rejected").exists()

    def offset(self):
        p = lookout.offset_path(self.home)
        return int(p.read_text()) if p.exists() else 0

    def scripts(self):
        return self.cap.read_text(encoding="utf-8").split("\n--FAKE-PSQL-END--\n")[:-1] if self.cap.exists() else []

    def staged(self, script):
        body = script.split("FROM STDIN WITH (FORMAT csv);\n", 1)[1].split("\n\\.\n", 1)[0]
        return list(csv.reader(io.StringIO(body)))


class Intent(ShipCase):
    def test_intent_clean_ship_advances_offset(self):
        self.write([ev(1), ev(2), ev(3), mark(1)])
        spool = self.home / "events.jsonl"
        rc, err = self._ship_stderr(events_spool=spool)
        self.assertEqual((rc, err, spool.exists()), (0, "", False))   # r3 Minor 1: a clean ship says nothing
        self.assertEqual(self.offset(), lookout.spool_path(self.home).stat().st_size)
        script = self.scripts()[0]
        rows = self.staged(script)
        self.assertEqual(sorted(r[1] for r in rows), ["cap_tick", "events", "events", "events"])
        self.assertIn("update cap_tick set last_ship_ok_at = now(), spool_bytes_unshipped = 0"
                      " where board = 'my-app';", script)
        self.assertTrue(script.startswith("\\set VERBOSITY verbose\nBEGIN;") and script.rstrip().endswith("COMMIT;"))

    def test_intent_docker_down_leaves_spool_alone(self):
        self.write([ev(1)])
        before = lookout.spool_path(self.home).read_bytes()
        os.environ["FAKE_PSQL_RC"] = "1"
        self.assertNotEqual(lookout_ship.ship(self.home), 0)
        self.assertEqual(self.offset(), 0)
        self.assertEqual(lookout.spool_path(self.home).read_bytes(), before)
        os.environ.update(FAKE_PSQL_RC="0", FAKE_PSQL_SLEEP="5")
        old = lookout_ship.TIMEOUT
        lookout_ship.TIMEOUT = 1
        try:
            t0 = time.monotonic()
            self.assertEqual(lookout_ship.ship(self.home), 124)
            self.assertLess(time.monotonic() - t0, 4)
        finally:
            lookout_ship.TIMEOUT = old
        self.assertEqual(self.offset(), 0)

    def test_intent_replay_inserts_nothing_new(self):
        self.write([ev(1), ev(2)])
        lookout_ship.ship(self.home)
        lookout.offset_path(self.home).write_text("0")
        lookout_ship.ship(self.home)
        first, second = self.scripts()
        self.assertEqual(self.staged(first), self.staged(second))     # prices ride only on the first
        self.assertIn("on conflict (event_id) where event_id is not null do nothing", second)

    def test_intent_older_snap_never_overwrites(self):
        self.write([card()])
        lookout_ship.ship(self.home)
        script = self.scripts()[0]
        for table in lookout_ship.TABLES:
            if table in ("prices", "events"):
                continue
            self.assertIn(f"where excluded.snap_ts >= {table}.snap_ts", script if table == "cap_cards" else
                          lookout_ship.build_script([], left={}, prices=None, tables=[table]))

    def test_intent_killed_mid_script_resends_cleanly(self):
        self.write([ev(1), card()])
        os.environ["FAKE_PSQL_RC"] = "137"
        self.assertEqual(lookout_ship.ship(self.home), 137)
        self.assertEqual(self.offset(), 0)
        os.environ["FAKE_PSQL_RC"] = "0"
        self.assertEqual(lookout_ship.ship(self.home), 0)
        killed, clean = self.scripts()
        self.assertEqual(killed, clean)

    def test_intent_second_shipper_exits_at_once(self):
        self.write([ev(1)])
        with open(lookout.lookout_dir(self.home) / "ship.lock", "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            self.assertEqual(lookout_ship.ship(self.home), 0)
        self.assertEqual(self.scripts(), [])
        self.assertEqual(self.offset(), 0)

    def test_intent_token_in_block_reason_is_scrubbed(self):
        self.write([card(reason=f"blocked on {TOKEN}")])
        lookout_ship.ship(self.home)
        script = self.scripts()[0]
        self.assertNotIn(TOKEN, script)
        self.assertIn("blocked on", script)

    def test_intent_unknown_version_stops_the_ship(self):
        self.write([ev(1)])
        good_end = lookout.spool_path(self.home).stat().st_size
        self.write([dict(ev(2), v=2), ev(3)])
        err = io.StringIO()
        old, sys.stderr = sys.stderr, err
        try:
            rc = lookout_ship.ship(self.home)
        finally:
            sys.stderr = old
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.offset(), good_end)
        self.assertIn("lookout_spool_bad", err.getvalue())
        self.assertEqual([r[1] for r in self.staged(self.scripts()[0])], ["events"])
        with open(lookout.spool_path(self.home), "a") as f:
            f.write(json.dumps({"v": 1, "t": "nonsense"}) + "\n")
        self.assertNotEqual(lookout_ship.ship(self.home), 0)

    def test_intent_two_hour_backlog_drains_in_order(self):
        self.write([mark(n) for n in range(240)])
        self.assertEqual(lookout_ship.ship(self.home), 0)
        script = self.scripts()[0]
        rows = self.staged(script)
        self.assertEqual(len(rows), 240)
        self.assertEqual([int(r[0]) for r in rows], list(range(240)))
        self.assertIn("order by board, seq desc", script)

    def test_intent_ships_in_bounded_chunks_advancing_offset_per_chunk(self):
        """T3 review r2 item 2: a big backlog no longer goes through one psql call for up to
        64 MB - it ships in bounded chunks, and the offset advances after each chunk lands
        rather than only once at the very end. Oracle 1: more than one psql call for a backlog
        over CHUNK_ROWS. Oracle 2: every row across every chunk lands exactly once and the final
        offset reaches the end of the spool."""
        old_rows = lookout_ship.CHUNK_ROWS
        lookout_ship.CHUNK_ROWS = 3
        try:
            self.write([ev(i) for i in range(7)])
            self.assertEqual(lookout_ship.ship(self.home), 0)
        finally:
            lookout_ship.CHUNK_ROWS = old_rows
        scripts = self.scripts()
        self.assertEqual(len(scripts), 3)                      # ceil(7 / 3): more than one psql call
        rows_per_script = [len(self.staged(s)) for s in scripts]
        self.assertEqual(rows_per_script, [3, 3, 1])
        self.assertEqual(self.offset(), lookout.spool_path(self.home).stat().st_size)

    def test_transport_failure_mid_chunk_leaves_the_rest_unshipped(self):
        """T3 review r2 item 2 (second half): a real transport failure (not a SQL/constraint
        error) partway through a multi-chunk ship stops the loop there - the offset lands at the
        end of the last chunk that actually landed, never at 0 (losing the whole progress) and
        never past the failed chunk (skipping unshipped bytes)."""
        old_rows = lookout_ship.CHUNK_ROWS
        lookout_ship.CHUNK_ROWS = 2
        self.write([ev(i) for i in range(6)])
        raw_lines = lookout.spool_path(self.home).read_bytes().split(b"\n")
        first_chunk_end = len(raw_lines[0]) + 1 + len(raw_lines[1]) + 1
        os.environ["FAKE_PSQL_FAIL_ON_CALL"] = "2"              # the second chunk (rows 2-3) fails
        try:
            rc = lookout_ship.ship(self.home)
        finally:
            lookout_ship.CHUNK_ROWS = old_rows
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.offset(), first_chunk_end)        # not 0, not the full 6-row length

    def test_intent_constraint_violation_quarantines_the_row_and_continues(self):
        """T3 review r2 item 1: a spool row that parses cleanly but a Postgres constraint rejects
        (NOT NULL, a check, ...) used to make the whole batched upsert - and so every row around
        it - fail forever. Oracle 1: the offending row lands in the `.rejected` sidecar with the
        psql error, and the good rows on either side still ship (offset reaches the end, ship()
        exits 0). Oracle 2: isolating the bad row costs exactly one whole-chunk attempt, one
        prices-free retry of the whole chunk (M-R2-2), plus one psql call per row (call count),
        not a silent swallow of the whole chunk."""
        self.write([ev(1), ev(2), ev(3)])
        os.environ["FAKE_PSQL_REJECT_IF_CONTAINS"] = "e2"      # unique to ev(2)'s event_id (csv-quotes
        self.assertEqual(lookout_ship.ship(self.home), 0)      # double the json quotes, so match on the id alone)
        self.assertEqual(self.offset(), lookout.spool_path(self.home).stat().st_size)
        rejected = [json.loads(l) for l in lookout.rejected_path(self.home).read_text().splitlines()]
        self.assertEqual(len(rejected), 1)
        self.assertIn("e2", rejected[0]["line"])
        self.assertIn("not-null constraint", rejected[0]["error"])
        self.assertEqual(len(self.scripts()), 5)      # 1 whole-chunk + 1 prices-free retry + 3 per-row

    def test_intent_systemic_sql_error_fails_loudly_with_no_quarantine(self):
        """Fix round 1 C1 (review P1): a systemic error - schema drift (42P10), not a bad row -
        is class 42, not 22/23, so it must never be isolated/quarantined: every retry would just
        fail the exact same way and the whole spool would silently vanish into `.rejected` while
        ship() reported success. The fixture uses the real VERBOSITY-verbose shape (round 2: the
        `42P10:` token plus a LOCATION line), so this exercises the actual parse path rather than
        a message with no SQLSTATE at all. Oracle 1: `.rejected` stays absent. Oracle 2: rc is
        the real psql failure and the offset does not move."""
        self.write([ev(1), ev(2), ev(3)])
        os.environ["FAKE_PSQL_RC"] = "3"
        os.environ["FAKE_PSQL_STDOUT_FILE"] = str(self.home / "err.txt")
        (self.home / "err.txt").write_text(
            "psql:<stdin>:9: ERROR:  42P10: there is no unique or exclusion constraint matching"
            " the ON CONFLICT specification\n"
            "LOCATION:  transformOnConflictClause, parse_clause.c:2557\n")
        self.assertEqual(lookout_ship.ship(self.home), 3)
        self.assertFalse(self.rejected_exists())
        self.assertEqual(self.offset(), 0)
        self.assertEqual(len(self.scripts()), 1)      # the first _recoverable gate stops it: no retries

    def test_intent_one_bad_row_with_a_systemic_code_still_fails_loudly(self):
        """Round 2 I-R2-2 guard (a), isolated from guard (b): _recoverable's SQLSTATE-class gate
        on its own. A MIXED chunk (2 good rows, 1 with a real error) where the bad row's own code
        is systemic (42P10, class 42, and its real message legitimately says "constraint" too -
        exactly why a word match is unreliable) must still refuse to isolate it, no matter how
        many OTHER rows in the chunk are fine. Oracle 1: `.rejected` stays absent. Oracle 2: rc is
        the real psql failure, offset unchanged."""
        self.write([ev(1), ev(2), ev(3)])
        os.environ["FAKE_PSQL_REJECT_IF_CONTAINS"] = "e2"
        os.environ["FAKE_PSQL_REJECT_SQLSTATE"] = "42P10"
        self.assertEqual(lookout_ship.ship(self.home), 3)
        self.assertFalse(self.rejected_exists())
        self.assertEqual(self.offset(), 0)

    def test_intent_every_row_failing_the_same_way_is_never_quarantined(self):
        """Round 2 I-R2-2 guard (b), isolated from guard (a): the all-rows-failed guard on its
        own. Every row fails with a genuinely recoverable code (23502) - the "a whole table
        stream is suddenly bad" case. N independently bad rows is far less likely than one
        systemic cause that happens to still share SQLSTATE class 23 (e.g. a dropped index), so
        nothing is quarantined and the real failure surfaces. Oracle 1: `.rejected` stays absent.
        Oracle 2: rc is the real psql failure, offset unchanged."""
        self.write([ev(1), ev(2), ev(3)])
        os.environ["FAKE_PSQL_REJECT_IF_CONTAINS"] = "my-app"    # every ev() row carries this board
        os.environ["FAKE_PSQL_REJECT_SQLSTATE"] = "23502"
        self.assertEqual(lookout_ship.ship(self.home), 3)
        self.assertFalse(self.rejected_exists())
        self.assertEqual(self.offset(), 0)

    def test_intent_prices_and_one_bad_row_only_quarantines_that_row(self):
        """Round 2 I-R2-2 guard (c), isolated from (a)/(b): the prices-exclusion guard on its
        own. Prices AND one row (e2) both fail; M-R2-2's retry-without-prices confirms the
        failure survives without prices (so it is not purely a prices problem), then per-row
        isolation (still without prices) finds exactly the one genuinely bad row. Oracle 1: only
        e2 lands in `.rejected`; e1/e3 still ship (offset reaches the end). Oracle 2: prices_at
        is not updated - the prices half of the failure is still real and retried next time."""
        self.write([ev(1), ev(2), ev(3)])
        os.environ["FAKE_PSQL_REJECT_IF_CONTAINS"] = "insert into prices|e2"
        os.environ["FAKE_PSQL_REJECT_SQLSTATE"] = "23502"
        self.assertEqual(lookout_ship.ship(self.home, full=True), 0)
        self.assertEqual(self.offset(), lookout.spool_path(self.home).stat().st_size)
        rejected = [json.loads(l) for l in lookout.rejected_path(self.home).read_text().splitlines()]
        self.assertEqual(len(rejected), 1)
        self.assertIn("e2", rejected[0]["line"])
        last = json.loads((lookout.lookout_dir(self.home) / "ship.last.json").read_text())
        self.assertIsNone(last.get("prices_at"))

    def _ship_stderr(self, **kw):
        err, old = io.StringIO(), sys.stderr
        sys.stderr = err
        try:
            rc = lookout_ship.ship(self.home, **kw)
        finally:
            sys.stderr = old
        return rc, err.getvalue()

    def test_intent_quarantine_is_announced_on_stderr(self):
        """Round 3 Important 2: a quarantined row exits 0, so the tick log must say so or a
        schema/collector drift goes silently stale. Oracle: one line with the count and the path."""
        self.write([ev(1), ev(2), ev(3)])
        os.environ["FAKE_PSQL_REJECT_IF_CONTAINS"] = "e2"
        rc, err = self._ship_stderr()
        self.assertEqual(rc, 0)
        self.assertIn(f"quarantined 1 row(s) to {lookout.rejected_path(self.home)}", err)

    def test_intent_prices_only_failure_is_announced_on_stderr(self):
        """Round 3 Important 2: the prices-free retry succeeding hides a prices failure behind
        rc 0; one stderr line carries the psql error so the tick log shows it."""
        self.write([ev(1), ev(2), ev(3)])
        os.environ["FAKE_PSQL_REJECT_IF_CONTAINS"] = "insert into prices"
        os.environ["FAKE_PSQL_REJECT_SQLSTATE"] = "23502"
        rc, err = self._ship_stderr(full=True)
        self.assertEqual(rc, 0)
        self.assertIn("lookout ship: prices failed, rows shipped without them:", err)
        self.assertIn("23502", err)

    def test_intent_bad_prices_entry_never_gets_blamed_on_rows(self):
        """Fix round 1 C1 (review P2), reshaped by round 2's M-R2-2: a bad `prices.json` entry
        fails the whole-chunk attempt (prices ride along with it). The chunk is retried ONCE more
        without prices before ever isolating rows (cheaper than N per-row `docker exec` calls,
        and makes a prices-only failure explicit); that retry succeeds here since every row is
        actually fine, so the good rows ship on their own merit rather than being held hostage by
        an unrelated prices.json bug. Oracle 1: `.rejected` stays absent - nothing was ever a bad
        ROW. Oracle 2: the rows ship (rc 0, offset at the end) but prices_at is not updated -
        prices are retried again on the next ship(), once prices.json is fixed."""
        self.write([ev(1), ev(2), ev(3)])
        os.environ["FAKE_PSQL_REJECT_IF_CONTAINS"] = "insert into prices"
        os.environ["FAKE_PSQL_REJECT_SQLSTATE"] = "23502"
        self.assertEqual(lookout_ship.ship(self.home, full=True), 0)
        self.assertFalse(self.rejected_exists())
        self.assertEqual(self.offset(), lookout.spool_path(self.home).stat().st_size)
        last = json.loads((lookout.lookout_dir(self.home) / "ship.last.json").read_text())
        self.assertIsNone(last.get("prices_at"))

    def test_intent_data_exception_row_quarantines_and_the_rest_ships(self):
        """Fix round 1 I1 (review P3): a data-exception SQLSTATE (class 22 - e.g. 22007, an
        invalid timestamp a malformed spool row pushed through jsonb_populate_record) is just as
        row-shaped as a class-23 constraint violation and must be isolated the same way. Oracle
        1: the bad row lands in `.rejected` with the real error; the good rows still ship (offset
        reaches the end, ship() exits 0). Oracle 2: isolation actually ran (more than the one
        whole-chunk psql call)."""
        self.write([ev(1), ev(2), ev(3)])
        os.environ["FAKE_PSQL_REJECT_IF_CONTAINS"] = "e2"
        os.environ["FAKE_PSQL_REJECT_SQLSTATE"] = "22007"
        self.assertEqual(lookout_ship.ship(self.home), 0)
        self.assertEqual(self.offset(), lookout.spool_path(self.home).stat().st_size)
        rejected = [json.loads(l) for l in lookout.rejected_path(self.home).read_text().splitlines()]
        self.assertEqual(len(rejected), 1)
        self.assertIn("e2", rejected[0]["line"])
        self.assertGreater(len(self.scripts()), 1)

    def test_intent_letter_bearing_sqlstate_row_quarantines_and_the_rest_ships(self):
        """Round 2 I-R2-1: SQLSTATEs are five characters from [0-9A-Z], not all-digit - 22P02
        (invalid_text_representation, what jsonb_populate_record raises for a bad int/bool/array
        value) is the single most likely real bad row, and a digits-only regex missed it, wedging
        forever exactly like I1's original bug. Oracle 1: the bad row lands in `.rejected`; the
        good rows still ship (offset at the end, exit 0). Oracle 2: isolation actually ran (more
        than the one whole-chunk psql call)."""
        self.write([ev(1), ev(2), ev(3)])
        os.environ["FAKE_PSQL_REJECT_IF_CONTAINS"] = "e2"
        os.environ["FAKE_PSQL_REJECT_SQLSTATE"] = "22P02"
        self.assertEqual(lookout_ship.ship(self.home), 0)
        self.assertEqual(self.offset(), lookout.spool_path(self.home).stat().st_size)
        rejected = [json.loads(l) for l in lookout.rejected_path(self.home).read_text().splitlines()]
        self.assertEqual(len(rejected), 1)
        self.assertIn("e2", rejected[0]["line"])
        self.assertGreater(len(self.scripts()), 1)

    def test_intent_bad_spool_line_exit_code_never_collides_with_psql(self):
        """T3 review r2 item 3: psql itself exits 3 (ON_ERROR_STOP=1) on any SQL error, so a bad
        spool line returning 3 too made "exit 3" ambiguous between a shipper-side spool problem
        and a Postgres-side one. Oracle 1: a bad line's own code is EXIT_SPOOL_BAD, not 3. Oracle
        2: a genuine psql SQL error (FAKE_PSQL_RC=3, a clean spool) still surfaces psql's own 3
        unchanged, so the two failure classes are now told apart by return code alone."""
        self.assertNotEqual(lookout_ship.EXIT_SPOOL_BAD, 3)
        line = card()
        line["row"]["surprise"] = 1
        self.write([line])
        self.assertEqual(lookout_ship.ship(self.home), lookout_ship.EXIT_SPOOL_BAD)
        home2 = Path(tempfile.mkdtemp())                    # fresh home: a clean spool, no bad line
        self.addCleanup(tmpclean.sweep, home2)
        lookout.lookout_dir(home2).mkdir(parents=True)
        with open(lookout.spool_path(home2), "a", encoding="utf-8") as f:
            f.write(json.dumps(ev(1), ensure_ascii=False) + "\n")
        os.environ["FAKE_PSQL_RC"] = "3"
        self.assertEqual(lookout_ship.ship(home2), 3)

    def test_file_matches_tests(self):
        text = (INTENT / "shipper.jsonl").read_text()
        sums = dict(reversed(l.split(maxsplit=1)) for l in (INTENT / "SHA256SUMS").read_text().splitlines())
        self.assertEqual(sums["shipper.jsonl"], hashlib.sha256(text.encode()).hexdigest())
        ids = {json.loads(l)["id"].replace("-", "_") for l in text.splitlines()}
        self.assertEqual(ids, {n[len("test_intent_"):] for n in dir(Intent) if n.startswith("test_intent_")})


class Contract(ShipCase):
    def test_psql_argv_is_the_exact_transport_command(self):
        """I2: the fake docker ignoring argv let `ON_ERROR_STOP=1` or the wrong container name pass
        every test silently - pin the real argv `psql()` invokes, not just what it piped to stdin."""
        argv_file = self.home / "argv.jsonl"
        os.environ["FAKE_DOCKER_ARGV"] = str(argv_file)
        self.write([ev(1)])
        self.assertEqual(lookout_ship.ship(self.home), 0)
        calls = [json.loads(l) for l in argv_file.read_text().splitlines()]
        self.assertEqual(calls[0], ["exec", "-i", lookout_ship.CONTAINER, "psql", "-U", "ops", "-d", "ops", "-q",
                                    "-v", "ON_ERROR_STOP=1", "-f", "-"])

    def test_csv_hazards_round_trip_in_captured_script(self):
        title = 'a, b "c"\nd é'
        self.write([card(title=title)])
        lookout_ship.ship(self.home)
        doc = json.loads(self.staged(self.scripts()[0])[0][2])
        self.assertEqual(doc["title"], title)
        self.assertNotIn("\r", self.scripts()[0])

    def test_unknown_row_key_is_a_bad_line(self):
        line = card()
        line["row"]["surprise"] = 1
        self.write([line])
        self.assertEqual(lookout_ship.ship(self.home), lookout_ship.EXIT_SPOOL_BAD)
        self.assertEqual(self.offset(), 0)

    def test_partial_last_line_is_left_for_the_next_ship(self):
        self.write([ev(1)], raw_tail='{"v": 1, "t": "ev')
        whole = lookout.spool_path(self.home).stat().st_size - len('{"v": 1, "t": "ev')
        self.assertEqual(lookout_ship.ship(self.home), 0)
        self.assertEqual(self.offset(), whole)

    def test_prices_are_upserted_from_prices_json_when_due(self):
        self.write([ev(1)])
        lookout_ship.ship(self.home)
        prices = json.loads((paths.REPO / "maintenance" / "prices.json").read_text())["models"]
        script = self.scripts()[0]
        for model in prices:
            self.assertIn(f"('{model}', ", script)
        self.assertIn("on conflict (model, valid_from) do update", script)

    def test_unshipped_bytes_left_after_a_bad_line_is_pinned(self):
        """m5: `left` must be the real remaining-bytes count, not forced to 0, so `cap_tick.
        spool_bytes_unshipped` (and status()'s unshipped_bytes once the bad line is fixed) tells
        the truth about how far behind Postgres is."""
        self.write([ev(1)])
        good_end = lookout.spool_path(self.home).stat().st_size
        self.write([dict(ev(2), v=2)])
        total = lookout.spool_path(self.home).stat().st_size
        self.assertEqual(lookout_ship.ship(self.home), lookout_ship.EXIT_SPOOL_BAD)
        script = self.scripts()[0]
        self.assertIn(f"update cap_tick set last_ship_ok_at = now(), spool_bytes_unshipped = {total - good_end}"
                      " where board = 'my-app';", script)

    def test_ship_last_json_persists_prices_at_so_a_replay_does_not_reprice(self):
        """m5: without a persisted `ship.last.json`, `_last(home)` would read back `{}` every time
        and `due` would be true on every ship (prices_at defaults to 0), so a replay would insert
        the prices block again - pin that the second ship's script has no `prices` upsert."""
        self.write([ev(1)])
        lookout_ship.ship(self.home)
        last = json.loads((lookout.lookout_dir(self.home) / "ship.last.json").read_text())
        self.assertEqual(last["rc"], 0)
        self.assertIsNotNone(last["prices_at"])
        self.write([ev(2)])
        lookout_ship.ship(self.home)
        first, second = self.scripts()
        self.assertIn("on conflict (model, valid_from) do update", first)
        self.assertNotIn("on conflict (model, valid_from) do update", second)

    def test_no_send_without_a_docker_override_refuses_to_ship(self):
        self.write([ev(1)])
        os.environ.pop("AGENT_OPS_DOCKER_BIN")
        os.environ["AGENT_OPS_NO_SEND"] = "1"
        self.assertEqual(lookout_ship.ship(self.home), 0)
        self.assertEqual(self.offset(), 0)
        self.assertEqual(self.scripts(), [])

    def test_maintenance_spool_ships_even_when_the_lookout_ship_raises(self):
        """N1: tick_health rides this events.ship() once lookout.ship is on; an exception must not starve it."""
        self.write([ev(1)])
        real_ship, real_parse = lookout_ship.events.ship, lookout_ship.parse
        seen = []
        lookout_ship.events.ship = lambda *a, **kw: seen.append("ship") or 0

        def broken(raw):
            raise RuntimeError("parser bug")
        lookout_ship.parse = broken
        try:
            with self.assertRaises(RuntimeError):
                lookout_ship.ship(self.home)
        finally:
            lookout_ship.events.ship, lookout_ship.parse = real_ship, real_parse
        self.assertEqual(seen, ["ship"])
        self.assertEqual(self.offset(), 0)

    def test_ship_full_asks_collect_for_a_resync(self):
        self.write([ev(1)])
        lookout_ship.ship(self.home, full=True)
        self.assertTrue((lookout.lookout_dir(self.home) / "resync.want").exists())

    def test_status_reports_unshipped_bytes(self):
        self.write([ev(1)])
        self.assertEqual(lookout_ship.status(self.home)["unshipped_bytes"],
                         lookout.spool_path(self.home).stat().st_size)

    def test_status_reports_the_quarantined_count(self):
        """Fix round 1 I2: a quarantine is invisible to an operator unless something surfaces the
        count - `status()` (and, through it, `capsule lookout ship --status` and any monitoring
        that reads it) now does."""
        self.assertEqual(lookout_ship.status(self.home)["rejected"], 0)
        self.write([ev(1), ev(2), ev(3)])
        os.environ["FAKE_PSQL_REJECT_IF_CONTAINS"] = "e2"
        lookout_ship.ship(self.home)
        self.assertEqual(lookout_ship.status(self.home)["rejected"], 1)

    def test_quarantine_dedupes_across_a_crash_before_the_offset_advances(self):
        """Fix round 1 I2: quarantine lands on disk before the chunk's offset advances (M1's
        ordering - same as any other row in the chunk). A crash right there - simulated here by
        making the offset file's write fail exactly once - means the next ship() reprocesses the
        identical, still-unshipped chunk from scratch and would re-find the same bad row; rows
        are keyed by byte span, so the rerun is a no-op rather than a duplicate entry. Exercised
        end to end through ship() only, never a private helper, so a full revert of this file is
        caught (mutation gate)."""
        self.write([ev(1), ev(2), ev(3)])
        os.environ["FAKE_PSQL_REJECT_IF_CONTAINS"] = "e2"

        class FlakyOffset:
            calls = 0

            def __init__(self, real):
                self._real = real

            def read_text(self):
                return self._real.read_text()

            def write_text(self, *a, **kw):
                FlakyOffset.calls += 1
                if FlakyOffset.calls == 1:
                    raise RuntimeError("simulated crash before the offset advances")
                return self._real.write_text(*a, **kw)

        flaky = FlakyOffset(lookout.offset_path(self.home))
        with unittest.mock.patch.object(lookout, "offset_path", return_value=flaky):
            with self.assertRaises(RuntimeError):
                lookout_ship.ship(self.home)
        self.assertEqual(self.offset(), 0)                              # the crash pre-empted the write
        first = lookout.rejected_path(self.home).read_text().splitlines()
        self.assertEqual(len(first), 1)                                 # quarantine had already landed

        self.assertEqual(lookout_ship.ship(self.home), 0)                # rerun over the identical bytes
        self.assertEqual(self.offset(), lookout.spool_path(self.home).stat().st_size)
        second = lookout.rejected_path(self.home).read_text().splitlines()
        self.assertEqual(len(second), 1)                                # deduped, not appended again

    def test_cli_ship_end_to_end(self):
        self.write([ev(1)])
        r = subprocess.run([sys.executable, "-m", "capsule", "lookout", "ship", "--capsule-home", str(self.home)],
                           cwd=str(paths.REPO), capture_output=True, text=True, timeout=60, env=dict(os.environ))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.offset(), lookout.spool_path(self.home).stat().st_size)


class Check(ShipCase):
    """Oracle 2 of spec 11.2: SQLite counts vs ops-pg counts for the same watermark."""

    def setUp(self):
        super().setUp()
        self.board_db = paths.board_db("my-app", self.home)
        db.create_board(self.board_db)
        conn = db.connect(self.board_db)
        a = db.new_task(conn, title="a", branch="epic/x")
        db.new_task(conn, title="b", branch="epic/x")
        with db.txn(conn):                                   # a legacy run: no run_id, no runner
            conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?,?,?,?)",
                         (a, "dev", "done", 1))
        top = conn.execute("SELECT MAX(id) FROM task_events").fetchone()[0]
        db.guard_set(conn, lookout.EVENTS_WATERMARK, {"event_id": top})
        conn.close()
        self.cfg = {"board": "my-app"}
        self.pg_out = self.home / "pg.txt"
        os.environ["FAKE_PSQL_STDOUT_FILE"] = str(self.pg_out)
        self.spool = self.home / "events.jsonl"

    GOOD = "cards|2\nruns|1\nruns_without_run_id|1\nevents|2\nopen_blocks|0\nopen_incidents|0\ncommits|0\n"

    def test_equal_counts_exit_0_and_the_a3_count_is_printed(self):
        self.pg_out.write_text(self.GOOD)
        out = io.StringIO()
        old, sys.stdout = sys.stdout, out
        try:
            rc = lookout_ship.check(self.home, self.cfg, events_spool=self.spool)
        finally:
            sys.stdout = old
        self.assertEqual(rc, 0)
        self.assertIn("A3 legacy-era runs without a run_id: 1 on the board, 1 shipped",
                      out.getvalue())
        self.assertFalse(self.spool.exists())

    def test_legacy_runs_missing_from_postgres_fail_loudly(self):
        self.pg_out.write_text(self.GOOD.replace("runs_without_run_id|1", "runs_without_run_id|0"))
        self.assertEqual(lookout_ship.check(self.home, self.cfg, events_spool=self.spool), 1)

    def test_open_blocks_count_the_board_not_the_collector(self):
        from capsule import blocks
        conn = db.connect(self.board_db)
        tid = db.new_task(conn, title="c", branch="epic/x")
        blocks.record_failure(conn, tid, reason="x", limit=1)          # parked in triage
        top = conn.execute("SELECT MAX(id) FROM task_events").fetchone()[0]
        db.guard_set(conn, lookout.EVENTS_WATERMARK, {"event_id": top})
        conn.close()
        self.pg_out.write_text(self.GOOD.replace("cards|2", "cards|3").replace("events|2", "events|4")
                               .replace("open_blocks|0", "open_blocks|1"))
        self.assertEqual(lookout_ship.check(self.home, self.cfg, events_spool=self.spool), 0)

    def test_a_guide_ruling_triage_is_no_open_block(self):
        """N3: tick.py parks a card in triage (block_kind guide_ruling) with no block event."""
        conn = db.connect(self.board_db)
        tid = db.new_task(conn, title="c", branch="epic/x")
        with db.txn(conn):
            conn.execute("UPDATE tasks SET status='triage', block_kind='guide_ruling' WHERE id=?", (tid,))
        top = conn.execute("SELECT MAX(id) FROM task_events").fetchone()[0]
        db.guard_set(conn, lookout.EVENTS_WATERMARK, {"event_id": top})
        conn.close()
        self.pg_out.write_text(self.GOOD.replace("cards|2", "cards|3").replace("events|2", "events|3"))
        self.assertEqual(lookout_ship.check(self.home, self.cfg, events_spool=self.spool), 0)

    def test_mismatch_exit_1_and_one_job_event(self):
        self.pg_out.write_text(self.GOOD.replace("cards|2", "cards|1"))
        self.assertEqual(lookout_ship.check(self.home, self.cfg, events_spool=self.spool), 1)
        rows = [json.loads(l) for l in self.spool.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["kind"], rows[0]["payload"]["job"], rows[0]["payload"]["exit"]),
                         ("job", "lookout-check", 1))
        self.assertIn("cards", rows[0]["text"])

    def test_a_non_integer_psql_line_is_an_error_not_a_crash(self):
        """m8: a stray psql notice line ('cards|NOTICE: ...') or an empty count must not raise
        ValueError out of check() - it is exit 2, the same as any other psql-side failure."""
        self.pg_out.write_text(self.GOOD.replace("cards|2", "cards|not-a-number"))
        self.assertEqual(lookout_ship.check(self.home, self.cfg, events_spool=self.spool), 2)


class Silence(unittest.TestCase):
    def setUp(self):
        self.got = {}
        self.status = 202   # Grafana 11.2's RouteCreateSilence answers 202, not 200 (I1)
        got, state = self.got, self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                got["path"] = self.path
                got["auth"] = self.headers.get("Authorization")
                got["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(state.status)
                self.end_headers()
                if state.status < 300:
                    self.wfile.write(b'{"silenceID":"s1"}')
                else:
                    self.wfile.write(b'{"message":"bad request"}')

            def log_message(self, *a):
                return None

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.tmp = Path(tempfile.mkdtemp())
        secret = self.tmp / "capsule-secret"
        secret.write_text("#!/bin/sh\nprintf 'pw-from-infisical'\n")
        secret.chmod(0o755)
        self.old = {k: os.environ.get(k) for k in ("LOOKOUT_GRAFANA_URL", "CAPSULE_SECRET_BIN")}
        os.environ.update(LOOKOUT_GRAFANA_URL=f"http://127.0.0.1:{self.srv.server_port}",
                          CAPSULE_SECRET_BIN=str(secret))

    def tearDown(self):
        self.srv.shutdown()
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        tmpclean.sweep(self.tmp)

    def test_silence_has_an_end_time(self):
        self.assertEqual(lookout_ship.silence("2h"), 0)
        body = self.got["body"]
        self.assertEqual(self.got["path"], "/api/alertmanager/grafana/api/v2/silences")
        self.assertEqual(body["matchers"][0], {"name": "__alert_rule_uid__",
                                               "value": "capsule-tick-deadman|factory-not-reporting",
                                               "isRegex": True, "isEqual": True})   # ruling (a)
        import datetime
        start = datetime.datetime.fromisoformat(body["startsAt"].replace("Z", "+00:00"))
        end = datetime.datetime.fromisoformat(body["endsAt"].replace("Z", "+00:00"))
        self.assertEqual(end - start, datetime.timedelta(hours=2))
        self.assertTrue(self.got["auth"].startswith("Basic "))

    def test_silence_accepts_any_2xx_from_grafana(self):
        """I1: Grafana 11.2's RouteCreateSilence answers 202 Accepted, not 200; the fake server's
        default here already matches that, so this pins the exact boundary against a regression."""
        self.status = 201
        self.assertEqual(lookout_ship.silence("30m"), 0)

    def test_silence_reports_a_4xx_from_grafana(self):
        self.status = 400
        self.assertEqual(lookout_ship.silence("30m"), 1)

    def test_silence_without_end_or_over_12h_is_refused(self):
        for bad in ("", "0", "0m", "13h", "forever", "2d"):
            with self.subTest(bad=bad):
                self.assertEqual(lookout_ship.silence(bad), 2)
        self.assertEqual(self.got, {})


class CollectorIntegration(ShipCase):
    """T2's fix round (df36ce8): `_append` guarantees no torn line ever lands in the spool (an
    interrupted write is ftruncated back), and `_payload` makes a poisoned `task_events.payload`
    skip that row entirely (F5) rather than writing garbage into the spool. Verifies the shipper's
    own reading of a spool `Collector.collect` actually produced still works end to end."""

    def test_ships_a_real_collect_pass_and_skips_a_payload_rejected_row(self):
        board_db = paths.board_db("my-app", self.home)
        db.create_board(board_db)
        conn = db.connect(board_db)
        tid = db.new_task(conn, title="a", branch="epic/x")
        with db.txn(conn):
            # a poisoned payload (valid JSON, not an object): _payload -> ({}, False), F5 skips the row
            conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
                         (tid, "completed", "[1, 2, 3]", int(time.time())))
            db.event(conn, tid, "claimed", {"ok": True})
        cfg = {"board": "my-app", "epics": ["epic/x"]}
        counts = {"lookout_errors": 0}
        collector = lookout.Collector(self.home, "my-app", host="h", pid=1)
        collector.collect(conn, cfg, time.time(), counts, pass_ms=10)
        conn.close()
        self.assertGreaterEqual(counts["lookout_errors"], 1)   # the poisoned row was counted...
        raw = lookout.spool_path(self.home).read_text(encoding="utf-8")
        lines = raw.splitlines()
        self.assertTrue(lines)
        for line in lines:                                     # ...and every spool line is whole JSON
            json.loads(line)
        payloads = [json.loads(l) for l in lines if json.loads(l).get("t") == "event"]
        self.assertTrue(all(p["row"].get("payload", {}).get("event") != "completed" for p in payloads))
        self.assertEqual(lookout_ship.ship(self.home), 0)       # the shipper ships the good rows cleanly
        self.assertEqual(self.offset(), lookout.spool_path(self.home).stat().st_size)


if __name__ == "__main__":
    unittest.main()
