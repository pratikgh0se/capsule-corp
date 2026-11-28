"""Factory autonomy Task 6 (spec 3.5): a board's tick is its only writer, so another board files a
card by dropping an intake file. Oracle 1: the intake_ingested event. Oracle 2: the archived file
on disk and the tasks row; provenance is checked on the source board itself."""
import argparse
import contextlib
import io
import json
import os
import shutil
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from capsule import db, intake, paths
from capsule.tests.private_names import DENYLIST, SKIP_MSG, project_name_re
from capsule.tests.test_tick import BRANCH, TickCase


class Intake(TickCase):
    def setUp(self):
        super().setUp()
        self.conn.execute("UPDATE tasks SET assignee=NULL WHERE id=?", (self.tid,))   # never dispatched here
        self.conn.commit()
        src = paths.board_db("src", self.home)                  # the board a touch happened on
        db.create_board(src)
        sconn = db.connect(src)
        self.src_task = db.new_task(sconn, title="card 7", status="blocked")
        self.inc = "inc_0000abcd"       # the row Task 7's opener writes; inserted directly (Task 6 lands first)
        sconn.execute("INSERT INTO incidents (id, board, task_id, kind, opened_at, evidence, root_key, fingerprint,"
                      " status) VALUES (?,?,?,?,?,?,?,?,?)", (self.inc, "src", self.src_task, "human_intervention", 1, "[]",
                                                             "human_touch: unblock: x", "0" * 16, "open"))
        sconn.commit()
        sconn.close()

    def drop(self, fp="unblock: needs_input/impl-hold", **kw):
        kw.setdefault("incident", self.inc)
        kw.setdefault("source_task", self.src_task)
        return intake.enqueue(self.home, "test", kind="human_touch", fingerprint=fp, title="Harness should have: x",
                              body="the body", source_board="src", **kw)

    def test_name_flows_from_intake_file_to_the_task(self):
        intake.enqueue(self.home, "test", kind="card", fingerprint="card", title="t", body="b",
                       source_board="operator", source_task=None, name="Short one")
        intake.ingest(self.conn, self.home, "test", self.cfg)
        row = self.conn.execute(
            "SELECT short_name, name_source FROM tasks WHERE title='t'").fetchone()
        self.assertEqual(tuple(row), ("Short one", "human"))

    def ingested(self):
        return [json.loads(r[0]) for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='intake_ingested' ORDER BY id")]

    def test_card_fields_flow_from_intake_flags_to_the_task_and_bad_ones_are_refused(self):
        from capsule.__main__ import main
        body = self.home / "card.md"
        body.write_text("a card")
        base = ["intake", "test", "--title", "fielded", "--body-file", str(body),
                "--capsule-home", str(self.home)]
        with contextlib.redirect_stdout(io.StringIO()):
            rc = main(base + ["--check", "make ok", "--expect", "ok", "--size", "s",
                              "--risk-flags", "public,big", "--workload", "research"])
        self.assertEqual(rc, 0)
        intake.ingest(self.conn, self.home, "test", self.cfg, now=1)
        row = self.conn.execute("SELECT * FROM tasks WHERE title='fielded'").fetchone()
        got = [row[c] for c in ("check_cmd", "check_expect", "size_tag", "workload", "passes")]
        self.assertEqual(got, ["make ok", "ok", "s", "research", None])
        self.assertEqual(json.loads(row["risk_flags"]), ["big", "public"])
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(base + ["--workload", "nonsense"]), 1)
        self.assertIn("workload", err.getvalue())

    def test_a_dropped_file_becomes_one_triage_card(self):
        rid = self.drop()
        self.assertTrue(rid.startswith("rq_"))
        counts = self.tick.once()
        self.assertEqual(counts["intake"], 1)                               # Z2: the heartbeat count, not just the event
        self.assertEqual([e["intake_id"] for e in self.ingested()], [rid])
        card = intake.card_for(self.conn, rid)
        self.assertEqual((card["status"], card["branch_name"]), ("triage", None))
        self.assertIn(f"intake: {rid}", card["body"])
        self.assertIn(self.inc, card["body"])
        self.assertTrue(intake.is_intake_card(self.conn, card["id"]))
        self.assertFalse(intake.is_intake_card(self.conn, self.tid))
        d = paths.board_dir("test", self.home) / "intake"
        self.assertTrue((d / "done" / f"{rid}.json").is_file())          # oracle 2: the file moved
        self.assertEqual([p.name for p in d.glob("*.json")], [])
        self.tick.once()
        self.assertEqual(len(self.ingested()), 1)                          # never twice

    def test_same_fingerprint_within_7_days_comments_instead(self):
        a = self.drop()
        self.tick.once()
        self.drop()
        self.tick.once()
        cards = self.conn.execute("SELECT COUNT(*) FROM tasks WHERE body LIKE '%intake: rq_%'").fetchone()[0]
        self.assertEqual(cards, 1)
        self.assertEqual(self.ingested()[-1]["deduped_into"], intake.card_for(self.conn, a)["id"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0], 1)

    def test_touch_epic_on_the_target_board_lands_ready_and_is_announced(self):
        self.cfg["autonomy"] = {"touch_epic": BRANCH, "touch_assignee": "dev-frontend"}
        rid = self.drop()
        intake.ingest(self.conn, self.home, "test", self.cfg, now=1)
        card = intake.card_for(self.conn, rid)
        self.assertEqual((card["status"], card["branch_name"], card["assignee"]), ("ready", BRANCH, "dev-frontend"))
        lines = [r[0] for r in self.conn.execute("SELECT body FROM outbox WHERE channel='file'")]
        self.assertTrue(any(card["id"] in ln and "ready on" in ln for ln in lines), lines)

    def test_a_triage_landing_hint_wins_over_touch_epic(self):
        self.cfg["autonomy"] = {"touch_epic": BRANCH, "touch_assignee": "dev-frontend"}
        rid = self.drop(landing="triage")
        intake.ingest(self.conn, self.home, "test", self.cfg, now=1)
        self.assertEqual(intake.card_for(self.conn, rid)["status"], "triage")

    def test_a_touch_file_naming_nothing_real_goes_to_bad(self):
        rid = self.drop(incident="inc_deadbeef")
        self.tick.once()
        d = paths.board_dir("test", self.home) / "intake"
        self.assertTrue((d / "bad" / f"{rid}.json").is_file())
        self.assertEqual(self.ingested(), [])

    def test_a_card_from_the_cli_lands_ready_and_never_dedupes(self):
        from capsule.__main__ import main
        body = self.home / "card.md"
        body.write_text("Task 3 of the factory-autonomy plan.")
        printed_ids = []
        for _ in range(2):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = main(["intake", "test", "--title", "autonomy task 3", "--body-file", str(body), "--epic", BRANCH,
                          "--assignee", "dev-frontend", "--file-set", "capsule/tick.py,capsule/amend.py",
                          "--capsule-home", str(self.home)])
            self.assertEqual(rc, 0)
            self.assertEqual(out.getvalue().count("\n"), 1)   # exactly the id, nothing else leaked to stdout
            printed_ids.append(out.getvalue().strip())
            self.assertTrue(printed_ids[-1].startswith("rq_"))
        intake.ingest(self.conn, self.home, "test", self.cfg, now=1)
        for pid in printed_ids:                               # second oracle: the printed id resolves to a card
            self.assertIsNotNone(intake.card_for(self.conn, pid))
        rows = self.conn.execute("SELECT status, branch_name, assignee, file_set FROM tasks WHERE title=?",
                                 ("autonomy task 3",)).fetchall()
        self.assertEqual(len(rows), 2)                                              # never deduped
        self.assertEqual((rows[0]["status"], rows[0]["branch_name"], rows[0]["assignee"]),
                         ("ready", BRANCH, "dev-frontend"))
        self.assertEqual(json.loads(rows[0]["file_set"]), ["capsule/tick.py", "capsule/amend.py"])

    def test_a_card_on_an_undeclared_epic_lands_in_triage(self):
        rid = intake.enqueue(self.home, "test", kind="card", fingerprint="card", title="t", body="b",
                             source_board="operator", source_task=None, epic="epic/nope", assignee="dev-frontend")
        intake.ingest(self.conn, self.home, "test", self.cfg, now=1)
        card = intake.card_for(self.conn, rid)
        self.assertEqual((card["status"], card["branch_name"]), ("triage", None))

    def test_a_backlog_filed_before_the_board_existed_is_ingested_in_one_pass(self):
        """C29: before the take, `capsule intake` only spools files under boards/<b>/intake/ (the
        board dir need not exist yet); the first tick pass after the take lands every one."""
        import tempfile
        fresh = Path(tempfile.mkdtemp(prefix="intake-backlog-"))
        self.addCleanup(shutil.rmtree, fresh, True)
        self.assertFalse((fresh / "boards").exists())
        rids = [intake.enqueue(fresh, "test", kind="card", fingerprint="card", title=f"t{i}",
                               body="b", source_board="operator", source_task=None)
                for i in range(4)]
        spool = fresh / "boards" / "test" / "intake"
        self.assertEqual(len(list(spool.glob("rq_*.json"))), 4)
        got = intake.ingest(self.conn, fresh, "test", self.cfg, now=1)
        self.assertEqual(len(got), 4)
        self.assertEqual(sorted(e["intake_id"] for e in self.ingested()), sorted(rids))
        self.assertEqual(len(list((spool / "done").glob("*.json"))), 4)

    def test_files_are_ingested_oldest_first(self):
        """Spec 3.5: per file, oldest first - the file's mtime decides, not its random id. The
        name order is deliberately the reverse of the mtime order, so a sort with no key (name
        order) would give the exactly wrong answer, never an accidentally-right one."""
        rids = [intake.enqueue(self.home, "test", kind="card", fingerprint=f"card{i}", title=f"t{i}", body="b",
                               source_board="operator", source_task=None) for i in range(3)]
        d = paths.board_dir("test", self.home) / "intake"
        by_name = sorted(rids)
        for i, rid in enumerate(by_name):                 # the first name (alphabetically) gets the newest mtime
            os.utime(d / f"{rid}.json", ns=((3 - i) * 10 ** 9,) * 2)
        intake.ingest(self.conn, self.home, "test", self.cfg, now=1)
        self.assertEqual([e["intake_id"] for e in self.ingested()], list(reversed(by_name)))

    def test_a_malformed_file_goes_to_bad(self):
        d = paths.board_dir("test", self.home) / "intake"
        d.mkdir(parents=True)
        (d / "rq_deadbeef.json").write_text("{nope")
        self.tick.once()
        self.assertTrue((d / "bad" / "rq_deadbeef.json").is_file())
        self.assertEqual(self.ingested(), [])

    def test_valid_json_with_the_wrong_keys_or_kind_goes_to_bad(self):
        d = paths.board_dir("test", self.home) / "intake"
        d.mkdir(parents=True)
        (d / "rq_badkeys1.json").write_text(json.dumps({"intake_id": "rq_badkeys1", "kind": "card"}))  # missing FIELDS
        doc = {"intake_id": "rq_badkind1", "kind": "not_a_real_kind", "fingerprint": "f", "title": "t", "body": "b",
              "source_board": "operator", "source_task": None, "incident": None, "epic": None, "assignee": None,
              "file_set": [], "landing": "auto", "at": 1}
        (d / "rq_badkind1.json").write_text(json.dumps(doc))
        self.tick.once()
        self.assertTrue((d / "bad" / "rq_badkeys1.json").is_file())
        self.assertTrue((d / "bad" / "rq_badkind1.json").is_file())
        self.assertEqual(self.ingested(), [])

    def test_an_extra_key_pins_the_shape_check(self):
        """Every FIELDS key plus one more must be refused by the shape check itself, not by
        provenance or a later KeyError - a card, so provenance never applies."""
        d = paths.board_dir("test", self.home) / "intake"
        d.mkdir(parents=True)
        doc = {"intake_id": "rq_extrakey1", "kind": "card", "fingerprint": "f", "title": "t", "body": "b",
              "source_board": "operator", "source_task": None, "incident": None, "epic": None, "assignee": None,
              "file_set": [], "landing": "auto", "at": 1, "extra": 1}
        (d / "rq_extrakey1.json").write_text(json.dumps(doc))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.tick.once()
        self.assertTrue((d / "bad" / "rq_extrakey1.json").is_file())
        self.assertEqual(self.ingested(), [])
        self.assertIn("bad intake document", err.getvalue())

    def test_an_unknown_kind_pins_the_shape_check(self):
        """A document with every FIELDS key and a real, checkable source (a guide-auto
        contract_amended event on its source task) must still be refused for its kind alone,
        not because provenance or dedupe would have refused it anyway."""
        sconn = db.connect(paths.board_db("src", self.home))
        sconn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
                     (self.src_task, "contract_amended", json.dumps({"by": "guide-auto"}), 1))
        sconn.commit()
        sconn.close()
        d = paths.board_dir("test", self.home) / "intake"
        d.mkdir(parents=True)
        doc = {"intake_id": "rq_badkind2", "kind": "not_a_real_kind", "fingerprint": "f", "title": "t", "body": "b",
              "source_board": "src", "source_task": self.src_task, "incident": None, "epic": None, "assignee": None,
              "file_set": [], "landing": "auto", "at": 1}
        (d / "rq_badkind2.json").write_text(json.dumps(doc))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.tick.once()
        self.assertTrue((d / "bad" / "rq_badkind2.json").is_file())
        self.assertEqual(self.ingested(), [])
        self.assertIn("bad intake document", err.getvalue())

    def test_an_unknown_landing_is_refused_not_silently_treated_as_auto(self):
        """A hand-written or foreign spool file naming a landing outside LANDINGS must not be
        treated as `auto` (which can land ready); it is refused like any other bad shape."""
        d = paths.board_dir("test", self.home) / "intake"
        d.mkdir(parents=True)
        doc = {"intake_id": "rq_badland1", "kind": "card", "fingerprint": "f", "title": "t", "body": "b",
              "source_board": "operator", "source_task": None, "incident": None, "epic": None, "assignee": None,
              "file_set": [], "landing": "Triage", "at": 1}
        (d / "rq_badland1.json").write_text(json.dumps(doc))
        self.tick.once()
        self.assertTrue((d / "bad" / "rq_badland1.json").is_file())
        self.assertEqual(self.ingested(), [])

    def test_a_right_shaped_but_wrongly_typed_file_goes_to_bad_not_a_tick_crash(self):
        """A schema-valid document whose value TYPES are wrong must park the file, never raise
        out of once()."""
        d = paths.board_dir("test", self.home) / "intake"
        d.mkdir(parents=True)
        doc = {"intake_id": "rq_badtype1", "kind": "human_touch", "fingerprint": "f", "title": "t", "body": "b",
              "source_board": "src", "source_task": ["not", "a", "scalar"], "incident": self.inc, "epic": None,
              "assignee": None, "file_set": [], "landing": "auto", "at": 1}
        (d / "rq_badtype1.json").write_text(json.dumps(doc))
        self.tick.once()                                 # must not raise
        self.tick.once()                                 # and the next pass must not re-raise either
        self.assertTrue((d / "bad" / "rq_badtype1.json").is_file())
        self.assertEqual(self.ingested(), [])

    def test_a_locked_database_is_retried_next_pass_not_parked(self):
        """A transient sqlite3 lock is not a bad document; the file must stay in the spool for
        the next pass, not be parked in bad/ forever - and the rest of this pass must not wedge:
        a file after the locked one is still ingested."""
        locked_rid = intake.enqueue(self.home, "test", kind="card", fingerprint="lockcard", title="locked",
                                    body="b", source_board="operator", source_task=None)
        ok_rid = intake.enqueue(self.home, "test", kind="card", fingerprint="okcard", title="ok", body="b",
                                source_board="operator", source_task=None)
        d = paths.board_dir("test", self.home) / "intake"
        os.utime(d / f"{locked_rid}.json", ns=(0, 0))                    # tried first: older
        os.utime(d / f"{ok_rid}.json", ns=(10 ** 9, 10 ** 9))
        real_new_task = db.new_task

        def side_effect(conn, **kwargs):
            if kwargs.get("title") == "locked":
                raise sqlite3.OperationalError("database is locked")
            return real_new_task(conn, **kwargs)

        with mock.patch("capsule.intake.db.new_task", side_effect=side_effect):
            touched = intake.ingest(self.conn, self.home, "test", self.cfg, now=1)
        self.assertTrue((d / f"{locked_rid}.json").is_file())            # left in place, not parked
        self.assertFalse((d / "bad" / f"{locked_rid}.json").exists())
        self.assertTrue((d / "done" / f"{ok_rid}.json").is_file())       # the pass kept going
        self.assertEqual(len(touched), 1)
        self.assertEqual([e["intake_id"] for e in self.ingested()], [ok_rid])

    def test_a_corrupt_source_db_goes_to_bad_not_a_tick_crash(self):
        corrupt = paths.board_db("corrupt", self.home)
        corrupt.parent.mkdir(parents=True)
        corrupt.write_bytes(b"not a sqlite file")
        rid = intake.enqueue(self.home, "test", kind="human_touch", fingerprint="f", title="t", body="b",
                             source_board="corrupt", source_task="t_whatever", incident="inc_x")
        self.tick.once()
        d = paths.board_dir("test", self.home) / "intake"
        self.assertTrue((d / "bad" / f"{rid}.json").is_file())
        self.assertEqual(self.ingested(), [])

    def test_redropping_an_already_ingested_file_is_a_noop(self):
        """Idempotency is on `intake_id` (a second row's event check), not on the spool's
        done/ move: the identical file, back in the spool a second time, must not double-file."""
        rid = self.drop()
        self.tick.once()
        d = paths.board_dir("test", self.home) / "intake"
        doc_text = (d / "done" / f"{rid}.json").read_text()
        (d / f"{rid}.json").write_text(doc_text)
        self.tick.once()
        self.assertEqual(len(self.ingested()), 1)
        self.assertTrue((d / "done" / f"{rid}.json").is_file())


class WorkloadHelp(unittest.TestCase):
    def test_workload_help_lists_exactly_the_workloads(self):
        from capsule import cli_intake
        for names in (("alpha", "beta"), ("upkeep",)):
            with mock.patch.object(cli_intake, "WORKLOADS", names):
                sub = argparse.ArgumentParser().add_subparsers()
                cli_intake.add_parser(sub)
                text = " ".join(sub.choices["intake"].format_help().split())
            self.assertIn("|".join(names), text)

    def test_help_matches_the_real_workloads(self):
        from capsule import card_fields, cli_intake
        sub = argparse.ArgumentParser().add_subparsers()
        cli_intake.add_parser(sub)
        text = " ".join(sub.choices["intake"].format_help().split())
        self.assertIn("|".join(card_fields.WORKLOADS), text)
        if not DENYLIST.is_file():
            self.skipTest(SKIP_MSG)   # the check above ran; only the private guard skips
        private = project_name_re()    # D226: no private project is a workload or named in the help
        self.assertEqual([w for w in card_fields.WORKLOADS if private.search(w)], [])
        self.assertIsNone(private.search(text))
