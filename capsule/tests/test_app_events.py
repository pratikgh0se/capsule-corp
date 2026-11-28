"""App failures as Korin incidents (D79, D89): capsule/app_events.py, its tick drain and its CLI.
Temp boards and temp homes only. Oracle 1: the incident row `open_incident` writes. Oracle 2: the
fingerprint recomputed here with hashlib from the spec's formula, not through `incidents`."""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import app_events, db, outcomes, paths, spool

DAY = 86400
IPS = os.path.realpath(      # evidence is stored resolved
    Path.home() / "Library/Logs/DiagnosticReports/MyAppBar-2026-10-28-210400.ips")
NESTED = "[" * 30_000 + "]" * 30_000                # under MAX_FILE, deep enough for RecursionError
T0 = 1_790_000_000 - 1_790_000_000 % DAY + 3600      # 01:00 UTC on some day


def event(**over):
    doc = {"schema": "app_event.v1", "source": "app:my-app", "kind": "crash",
           "signature": "EXC_BAD_ACCESS SIGSEGV TimerEngine.tick at 0x1f00a",
           "event_id": "5D1C2B7E-1F0A-4C55-9B2E-0C9F1E7A3D11",
           "evidence": [IPS],
           "version": "1.4.0", "via": "diagnostic_report"}
    doc.update(over)
    return doc


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        db.create_board(self.dir / "kanban.db")
        self.conn = db.connect(self.dir / "kanban.db")
        self.addCleanup(self.conn.close)

    def take(self, now=T0, **over):
        return app_events.intake(self.conn, board="my-app", doc=event(**over), now=now,
                                 app_dir=self.dir / "app-events")

    def row(self, incident_id):
        rec = dict(self.conn.execute("SELECT * FROM incidents WHERE id=?",
                                     (incident_id,)).fetchone())
        rec["evidence"] = json.loads(rec["evidence"])
        return rec

    def count(self):
        return self.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]


class Intake(Base):
    def test_a_crash_opens_a_crash_incident_with_the_app_as_role(self):
        inc, action = self.take()
        rec = self.row(inc)
        self.assertEqual((action, rec["kind"], rec["role"], rec["status"], rec["board"]),
                         ("opened", "crash", "app:my-app", "open", "my-app"))
        self.assertEqual(rec["evidence"], [
            "app_event:app:my-app:5D1C2B7E-1F0A-4C55-9B2E-0C9F1E7A3D11",
            IPS, "app_event_count: 1"])
        # second oracle: the spec's formula by hand (address masked to '#', lowercased)
        key = b"crash: exc_bad_access sigsegv timerengine.tick at #"
        want = hashlib.sha256(b"crash\napp:my-app\n" + key).hexdigest()[:16]
        self.assertEqual(rec["fingerprint"], want)

    def test_the_same_crash_at_another_address_the_same_day_attaches(self):
        a, _ = self.take()
        b, action = self.take(now=T0 + 3600, event_id="E2",
                              signature="EXC_BAD_ACCESS SIGSEGV TimerEngine.tick at 0x2b11c")
        self.assertEqual((b, action, self.count()), (a, "deduped", 1))
        self.assertIn("app_event:app:my-app:E2", self.row(a)["evidence"])

    def test_the_same_event_twice_is_seen_and_changes_nothing_even_days_later(self):
        a, _ = self.take()
        before = self.row(a)
        for now in (T0 + 60, T0 + 5 * DAY):
            self.assertEqual(self.take(now=now), (a, "seen"))
        self.assertEqual((self.count(), self.row(a)), (1, before))

    def test_a_closed_crash_back_within_30_days_is_a_repeat_with_one_outbox_line(self):
        a, _ = self.take()
        self.conn.execute("UPDATE incidents SET status='closed', closed_at=? WHERE id=?",
                          (T0 + DAY, a))
        b, action = self.take(now=T0 + 10 * DAY, event_id="E3")
        rec = self.row(b)
        self.assertEqual((action, rec["kind"], rec["repeat_of"], rec["role"]),
                         ("repeat", "repeat", a, "app:my-app"))
        lines = self.conn.execute(
            "SELECT body FROM outbox WHERE channel != 'local' AND body LIKE '%REPEAT%'").fetchall()
        self.assertEqual(len(lines), 1)

    def test_kinds_map_onto_korins_and_never_share_a_fingerprint(self):
        got = {}
        for i, kind in enumerate(("crash", "hang", "down", "error")):
            inc, action = self.take(kind=kind, event_id=f"K{i}")
            rec = self.row(inc)
            self.assertEqual(action, "opened")
            got[kind] = (rec["kind"], rec["fingerprint"])
        self.assertEqual({k: v[0] for k, v in got.items()},
                         {"crash": "crash", "hang": "hang", "down": "hang",
                          "error": "silent_failure"})
        self.assertEqual(len({v[1] for v in got.values()}), 4)

    def test_an_app_crash_never_shares_a_fingerprint_with_another_app_or_the_factory(self):
        a, _ = self.take()
        b, action = self.take(source="app:dojo", event_id="D1")
        self.assertEqual(action, "opened")
        self.assertNotEqual(self.row(a)["fingerprint"], self.row(b)["fingerprint"])

    def test_a_malformed_event_names_every_problem_and_opens_nothing(self):
        cases = {"schema": {"schema": "app_event.v2"}, "source": {"source": "my-app"},
                 "source ": {"source": "app:My-app"}, "kind": {"kind": ["crash"]},
                 "signature": {"signature": "  "}, "event_id": {"event_id": "a\nb"},
                 "event_id ": {"event_id": "x" * 201}, "evidence": {"evidence": "/one/path"}}
        for field, over in cases.items():
            with self.subTest(field), self.assertRaises(ValueError) as caught:
                self.take(**over)
            self.assertIn(field.strip(), str(caught.exception))
        with self.assertRaises(ValueError):
            app_events.intake(self.conn, board="my-app", doc=["not", "an", "object"],
                              app_dir=self.dir)
        self.assertEqual(self.count(), 0)

    def test_control_characters_and_long_text_are_refused_in_every_field(self):
        cases = {"event_id": {"event_id": "a\x07b"}, "signature": {"signature": "x" * 501},
                 "message": {"message": "line\nbreak"}, "app": {"app": "a\x85b"},
                 "evidence": {"evidence": [IPS + "\x1b[2J"]}, "version": {"version": "v" * 101},
                 "signature ": {"signature": "boom\x00"}}
        for field, over in cases.items():
            with self.subTest(field), self.assertRaises(ValueError) as caught:
                self.take(**over)
            self.assertIn(field.strip(), str(caught.exception))
        self.assertEqual(self.count(), 0)

    def test_evidence_outside_the_allowlist_is_dropped_with_a_counted_note(self):
        secret = str(Path.home() / ".config/capsule/x.env")
        app_dir = self.dir / "app-events"
        inside = os.path.realpath(app_dir / "done" / "a.json")
        junk = [secret, "/", "event:t_1", "app_event:app:my-app:E9", "relative/x.ips",
                IPS + "/../../../../.config/x", "/etc/passwd", "y" * 1100]
        inc, _ = app_events.intake(self.conn, board="my-app", app_dir=app_dir, now=T0,
                                   doc=event(evidence=[IPS, *junk, inside] + [IPS + f"{i}"
                                                                          for i in range(12)]))
        ev = self.row(inc)["evidence"]
        self.assertEqual(ev[1:3], [IPS, inside])
        home, mine = os.path.realpath(Path.home()), os.path.realpath(app_dir)
        self.assertTrue(all(e.startswith(("app_event", home, mine)) for e in ev))
        self.assertEqual(len([e for e in ev if e.startswith(home)]), 9)
        self.assertEqual(len([e for e in ev if e.startswith(mine)]), 1)
        self.assertEqual([e for e in ev if e.startswith("app_event_note")],
                         ["app_event_note: dropped 12 evidence item(s)"])
        self.assertNotIn(secret, ev)

    def test_a_flood_of_one_fingerprint_is_one_bounded_incident_with_a_count(self):
        first, _ = self.take(event_id="F0")
        for i in range(1, 300):
            inc, action = self.take(now=T0 + i, event_id=f"F{i}")
            self.assertEqual((inc, action), (first, "deduped"))
        rec = self.row(first)
        self.assertEqual((self.count(), len(rec["evidence"])), (1, app_events.MAX_ENTRIES + 1))
        self.assertEqual(rec["evidence"][-1], "app_event_count: 300")
        self.assertEqual(rec["evidence"][:2], ["app_event:app:my-app:F0", IPS])
        self.assertEqual(self.take(event_id="F0"), (first, "seen"))

    def test_every_event_is_seen_again_even_after_its_token_left_the_capped_evidence(self):
        ids = {self.take(now=T0 + i, event_id=f"S{i}")[0] for i in range(25)}
        self.assertEqual((len(ids), self.count()), (1, 1))
        before = self.row(next(iter(ids)))
        self.assertEqual(before["evidence"][-1], "app_event_count: 25")
        self.assertNotIn("app_event:app:my-app:S24", before["evidence"])     # trimmed by the cap
        again = [self.take(now=T0 + 100 + i, event_id=f"S{i}") for i in range(25)]
        self.assertEqual({a for _, a in again}, {"seen"})
        self.assertEqual((self.count(), self.row(next(iter(ids)))), (1, before))

    def test_the_seen_row_is_written_with_the_incident_and_rolls_back_with_it(self):
        inc, _ = self.take(event_id="L1")
        rows = self.conn.execute("SELECT token, incident_id, at FROM app_event_seen").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("app_event:app:my-app:L1", inc, T0)])
        with mock.patch.object(app_events, "_settle", side_effect=RuntimeError("boom")), \
                self.assertRaises(RuntimeError):
            self.take(event_id="L2", signature="another")
        self.assertEqual((self.count(), self.conn.execute(
            "SELECT COUNT(*) FROM app_event_seen").fetchone()[0]), (1, 1))

    def test_the_seen_table_migrates_onto_an_existing_board_and_migrating_again_is_harmless(self):
        inc, _ = self.take(event_id="M1")
        self.conn.execute("DROP TABLE app_event_seen")
        self.assertTrue(db.migrations_pending(self.conn))
        db.migrate(self.conn)
        self.assertFalse(db.migrations_pending(self.conn))
        self.conn.execute("INSERT INTO app_event_seen VALUES ('t', ?, 1)", (inc,))
        db.migrate(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM app_event_seen").fetchone()[0], 1)

    def test_evidence_is_stored_as_its_resolved_path_and_a_numeric_version_is_fine(self):
        app_dir = self.dir / "app-events"
        odd = str(app_dir / "x" / ".." / "done" / "a.json")
        inc, _ = app_events.intake(self.conn, board="my-app", app_dir=app_dir, now=T0,
                                   doc=event(evidence=[odd], version=1.4))
        self.assertEqual(self.row(inc)["evidence"][1],
                         str(Path(app_dir).resolve() / "done" / "a.json"))
        with self.assertRaises(ValueError):
            self.take(event_id="V2", version=[1])


class Drain(Base):
    def setUp(self):
        super().setUp()
        self.inbox = app_events.inbox(self.dir / "boards" / "my-app")
        self.inbox.mkdir(parents=True)

    def put(self, name, doc):
        (self.inbox / name).write_text(doc if isinstance(doc, str) else json.dumps(doc))

    def test_valid_files_go_to_done_bad_ones_to_rejected_and_tmp_files_wait(self):
        self.put("a.json", event())
        self.put("b.json", "{not json")
        self.put("c.json", event(kind="meltdown"))
        self.put("d.json.tmp", event(event_id="still-being-written"))
        ids = app_events.drain(self.conn, board="my-app", inbox_dir=self.inbox, now=T0)
        self.assertEqual(len(ids), 1)
        board = self.inbox.parent
        self.assertEqual(sorted(p.name for p in (board / "done").iterdir()), ["a.json"])
        self.assertEqual(sorted(p.name for p in (board / "rejected").iterdir()),
                         ["b.json", "c.json"])
        self.assertEqual([p.name for p in self.inbox.iterdir()], ["d.json.tmp"])
        self.assertIn(str(board / "done" / "a.json"), self.row(ids[0])["evidence"])

    def test_a_file_dropped_again_is_seen_and_not_counted(self):
        self.put("a.json", event())
        first = app_events.drain(self.conn, board="my-app", inbox_dir=self.inbox, now=T0)
        self.put("a.json", event())
        again = app_events.drain(self.conn, board="my-app", inbox_dir=self.inbox, now=T0 + DAY)
        self.assertEqual((len(first), again, self.count()), (1, [], 1))
        self.assertEqual(list(self.inbox.iterdir()), [])

    def test_a_recursion_error_while_parsing_rejects_the_file_and_the_next_one_goes_on(self):
        self.put("a.json", event(event_id="R1"))
        self.put("b.json", event(event_id="R2", signature="other"))
        real = json.loads
        calls = []

        def loads(text, *a, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise RecursionError("maximum recursion depth exceeded")
            return real(text, *a, **kw)
        with mock.patch.object(app_events.json, "loads", side_effect=loads):
            ids = app_events.drain(self.conn, board="my-app", inbox_dir=self.inbox, now=T0)
        self.assertEqual((len(ids), self.count()), (1, 1))
        self.assertEqual([p.name for p in (self.inbox.parent / "rejected").iterdir()], ["a.json"])

    def test_a_drain_pass_forgets_tokens_older_than_30_days_and_keeps_newer_ones(self):
        self.take(event_id="P1")
        self.take(now=T0 + 20 * DAY, event_id="P2", signature="other")
        app_events.drain(self.conn, board="my-app", inbox_dir=self.inbox, now=T0 + 31 * DAY)
        self.assertEqual([r[0] for r in self.conn.execute("SELECT token FROM app_event_seen")],
                         ["app_event:app:my-app:P2"])

    def _drain_with(self, failure):
        real = app_events.intake
        calls = []

        def intake(*a, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise failure
            return real(*a, **kw)
        self.put("a.json", event(event_id="D1"))
        self.put("b.json", event(event_id="D2", signature="other"))
        with mock.patch.object(app_events, "intake", side_effect=intake):
            return app_events.drain(self.conn, board="my-app", inbox_dir=self.inbox, now=T0)

    def test_a_busy_database_stops_the_pass_and_leaves_every_remaining_file(self):
        ids = self._drain_with(sqlite3.OperationalError("database is locked"))
        self.assertEqual((ids, self.count()), ([], 0))
        self.assertEqual(sorted(p.name for p in self.inbox.iterdir()), ["a.json", "b.json"])
        self.assertFalse((self.inbox.parent / "rejected").exists())

    def test_any_other_database_error_rejects_that_file_and_the_pass_goes_on(self):
        ids = self._drain_with(sqlite3.IntegrityError("constraint failed"))
        self.assertEqual((len(ids), self.count()), (1, 1))
        self.assertEqual([p.name for p in (self.inbox.parent / "rejected").iterdir()], ["a.json"])
        self.assertEqual([p.name for p in (self.inbox.parent / "done").iterdir()], ["b.json"])

    def test_one_pass_takes_at_most_max_per_pass_oldest_name_first(self):
        for i in range(3):
            self.put(f"{i}.json", event(event_id=f"N{i}", signature=f"sig {chr(97 + i)}"))
        with mock.patch.object(app_events, "MAX_PER_PASS", 2):
            ids = app_events.drain(self.conn, board="my-app", inbox_dir=self.inbox, now=T0)
        self.assertEqual((len(ids), [p.name for p in self.inbox.iterdir()]), (2, ["2.json"]))


class TickPass(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        db.create_board(paths.board_db("my-app", self.home))
        self.conn = db.connect(paths.board_db("my-app", self.home))
        self.addCleanup(self.conn.close)
        self.inbox = app_events.inbox(paths.board_dir("my-app", self.home))
        self.inbox.mkdir(parents=True)

    def tick(self):
        counts = {"tick_errors": 0}
        outcomes.tick_pass(self.conn, {"board": "my-app"}, {"main_checkout": self.home}, counts,
                           spool_root=None, human_dir=spool.human_dir(self.home, "my-app"),
                           now=T0)
        return counts

    def test_the_tick_drains_the_boards_inbox_and_counts_the_incident(self):
        conn, inbox = self.conn, self.inbox
        (inbox / "a.json").write_text(json.dumps(event()))
        self.assertEqual(self.tick()["incidents"], 1)
        rows = conn.execute("SELECT kind, role FROM incidents").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("crash", "app:my-app")])
        self.assertTrue((inbox.parent / "done" / "a.json").is_file())

    def test_a_bad_json_or_unreadable_inbox_entry_never_raises_out_of_the_tick(self):
        home, conn, inbox = self.home, self.conn, self.inbox
        outside = home / "outside.txt"
        outside.write_text("secret")
        (inbox / "a.json").write_text("{not json")
        (inbox / "b.json").write_bytes(b"\xff\xfe\x00")
        (inbox / "c.json").write_text(NESTED)               # RecursionError
        (inbox / "d.json").write_text(" " * (app_events.MAX_FILE + 1))   # oversize
        (inbox / "e.json").symlink_to(outside)
        os.mkfifo(inbox / "f.json")
        (inbox / "g.json").mkdir()
        (inbox / "h.json").write_text(json.dumps(event(signature="boom\x00")))
        (inbox / "z.json").write_text(json.dumps(event()))
        counts = self.tick()
        self.assertEqual(counts["incidents"], 1)
        self.assertEqual(sorted(p.name for p in (inbox.parent / "rejected").iterdir()),
                         ["a.json", "b.json", "c.json", "d.json", "e.json", "f.json", "g.json",
                          "h.json"])
        self.assertTrue((inbox.parent / "done" / "z.json").is_file())
        self.assertEqual((outside.read_text(), list(inbox.iterdir())), ("secret", []))

    def test_a_done_file_never_overwrites_an_earlier_one_of_the_same_name(self):
        conn, inbox = self.conn, self.inbox
        for n in (1, 2):
            (inbox / "a.json").write_text(json.dumps(event(event_id=f"U{n}")))
            app_events.drain(conn, board="my-app", inbox_dir=inbox, now=T0)
        done = [json.loads(p.read_text())["event_id"] for p in (inbox.parent / "done").iterdir()]
        self.assertEqual(sorted(done), ["U1", "U2"])


class Cli(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        cfg = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        cfg.update(board="my-app", korin={"main_checkout": str(self.home)})
        paths.config_path(self.home, "my-app").parent.mkdir(parents=True, exist_ok=True)
        paths.config_path(self.home, "my-app").write_text(json.dumps(cfg))
        db.create_board(paths.board_db("my-app", self.home))

    def run_cli(self, *argv):
        return subprocess.run([sys.executable, "-m", "capsule", "incidents", "app-event", *argv,
                               "--board", "my-app", "--capsule-home", str(self.home)],
                              cwd=paths.REPO, capture_output=True, text=True, timeout=30)

    def test_one_file_then_the_same_file_again(self):
        path = self.home / "e.json"
        path.write_text(json.dumps(event()))
        first, second = self.run_cli(str(path)), self.run_cli(str(path))
        self.assertEqual((first.returncode, second.returncode), (0, 0),
                         first.stderr + second.stderr)
        inc, action = first.stdout.split()
        self.assertEqual((action, second.stdout.split()), ("opened", [inc, "seen"]))
        self.assertTrue(path.is_file())                 # a hand-run file stays where it is

    def test_no_file_drains_the_inbox_and_a_bad_file_is_refused(self):
        inbox = app_events.inbox(paths.board_dir("my-app", self.home))
        inbox.mkdir(parents=True)
        (inbox / "a.json").write_text(json.dumps(event()))
        drained = self.run_cli()
        self.assertEqual(drained.returncode, 0, drained.stderr)
        self.assertIn("1 incident(s)", drained.stdout)
        bad = self.home / "bad.json"
        bad.write_text(json.dumps(event(source="my-app")))
        refused = self.run_cli(str(bad))
        self.assertEqual(refused.returncode, 1)
        self.assertIn("source is not app:<name>", refused.stderr)
        self.assertEqual(self.run_cli(str(self.home / "missing.json")).returncode, 1)
        deep = self.home / "deep.json"
        deep.write_text(NESTED)
        deep_run = self.run_cli(str(deep))
        self.assertEqual(deep_run.returncode, 1)
        self.assertNotIn("Traceback", deep_run.stderr)
        self.assertEqual(len(deep_run.stderr.strip().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
