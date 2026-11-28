"""t_092732be: an optional `author` (the owner | "cos") on intervention.v1 spool files, so Q2b stops
counting the chief of staff's interventions as owner touches.
Oracle 1: the real writer (`human.write`, the `capsule human` CLI) puts the author into the file and
the schema accepts it. Oracle 2: the real Q2b reader (`touches.evaluate`) counts the files by hand.
A legacy file with no author stays valid and counts as the owner's."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import db, human, paths, row_schemas, spool
from capsule.__main__ import main
from capsule.tests.private_names import DENYLIST, project_names
from capsule.tests.test_evals_report import DAY_START, trap_board, trap_eval
from capsule.tests.test_main_override import _cfg_home, _identity_dir

GOOD = {"task_id": "t_0123abcd", "kind": "unblock", "text": "go", "at": 1790000000}
AT = DAY_START + 5


def problems(doc):
    return row_schemas.problems("intervention.v1", doc)


class Schema(unittest.TestCase):
    def test_author_is_optional_and_exactly_owner_or_cos_with_the_legacy_value(self):
        schema = row_schemas.load("intervention.v1")
        self.assertEqual(schema["properties"]["author"]["enum"],
                         ["owner", human.LEGACY_OWNER, "cos", "capsule"])    # `capsule`: the patrol
        self.assertNotIn("author", schema["required"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(human.AUTHORS, ("owner", "cos"))      # the writer's two roles

    def test_every_capsule_authored_intervention_is_routine(self):
        from capsule import routine_nudges
        for kind in ("answer", "unblock", "reset"):
            self.assertTrue(routine_nudges.is_routine_intervention(
                None, "t_0123abcd", 1, {"kind": kind, "author": "capsule"}), kind)

    def test_old_file_without_author_still_validates_and_both_authors_do(self):
        self.assertEqual(problems(GOOD), [])
        for who in ("owner", human.LEGACY_OWNER, "cos"):
            self.assertEqual(problems(dict(GOOD, author=who)), [], who)
        # every value the schema allows reads as one of the two roles through the writer
        with terminal(), session():
            got = [human.cli_author(who) for who in ("owner", human.LEGACY_OWNER, "cos")]
        self.assertEqual(got, ["owner", "owner", "cos"])

    def test_other_authors_are_rejected(self):
        # the enum is case-sensitive: the capitalised legacy value is out
        for bad in ("", "Alex", human.LEGACY_OWNER.title(), "robot", None, 1):
            self.assertTrue(problems(dict(GOOD, author=bad)), repr(bad))


class Writer(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)

    def write(self, **kw):
        path = human.write(self.home, "b", "t_1", "unblock", "go", now=5, **kw)
        return json.loads(path.read_text())

    def cli(self, *extra):
        argv = ["human", "b", "t_1", "unblock", "go", "--capsule-home", str(self.home), *extra]
        with session(), terminal(), contextlib.redirect_stdout(io.StringIO()) as out:
            rc = main(argv)
        return rc, json.loads(Path(out.getvalue().strip()).read_text())

    def test_default_author_is_owner_and_cos_is_written_through(self):
        # the module fixture sets owner.name Alex
        self.assertEqual(self.write()["author"], "owner")
        self.assertEqual(self.write(author="cos")["author"], "cos")

    def test_a_host_named_like_the_legacy_value_still_writes_owner(self):
        # b114: the owner's configured name never reaches a file; only owner or cos is written
        with mock.patch.dict(os.environ, {"CAPSULE_OWNER_NAME": human.LEGACY_OWNER}):
            human._owner_name.cache_clear()
            try:
                self.assertEqual(self.write()["author"], "owner")
                self.assertEqual(self.write(author=human.LEGACY_OWNER)["author"], "owner")
            finally:
                human._owner_name.cache_clear()

    def test_legacy_value_round_trips_to_owner_on_read(self):
        self.assertEqual(human.canonical_author(human.LEGACY_OWNER), "owner")
        self.assertEqual(human.canonical_author(human.LEGACY_OWNER.title()), "owner")
        once = human.canonical_author(human.LEGACY_OWNER)
        self.assertEqual(human.canonical_author(once), "owner")
        self.assertEqual(human.canonical_author("cos"), "cos")

    def test_an_unknown_author_is_refused_before_a_file_exists(self):
        with self.assertRaises(ValueError):
            self.write(author="robot")
        self.assertEqual(list(spool.human_dir(self.home, "b").glob("*.json")), [])

    def test_cli_author_flag_defaults_to_owner_and_accepts_cos(self):
        for extra, want in (((), "owner"), (("--author", "cos"), "cos"),
                            (("--author", "owner"), "owner"),
                            (("--author", human.LEGACY_OWNER), "owner"),
                            (("--author", "Alex"), "owner")):
            rc, doc = self.cli(*extra)
            self.assertEqual((rc, doc["author"]), (0, want), extra)

    def test_cli_refuses_an_unknown_author(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.cli("--author", "robot")


class OverridePath(unittest.TestCase):
    """The override verb writes its file through its own call in `_human_override`: `--author`
    must reach it too (row 602 loophole hunt)."""

    def run_override(self, *extra):
        home = _cfg_home(["lint"])
        env = {k: v for k, v in os.environ.items() if k not in MARKERS}
        env.update(CAPSULE_IDENTITY="owner", CAPSULE_IDENTITY_DIR=str(_identity_dir()))
        argv = [sys.executable, "-m", "capsule", "human", "test", "t_x", "override", "reason",
                "--check", "lint", "--ttl", "72h", "--fix-card", "t_fix1",
                "--capsule-home", str(home), *extra]
        r = subprocess.run(argv, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(Path(r.stdout.strip()).read_text())

    def test_override_carries_author_cos_and_a_non_terminal_default_is_cos(self):
        self.assertEqual(self.run_override("--author", "cos")["author"], "cos")
        self.assertEqual(self.run_override()["author"], "cos")     # stdin is /dev/null: no person


MARKERS = ("CLAUDECODE", "AI_AGENT")      # an agent session sets them; a person's terminal doesn't


# every card verb in `capsule human --help`, plus a free-form note kind
VERBS = ("reset", "unblock", "answer", "accept", "screen-ok", "screen-fix", "epic", "park", "close",
         "amend", "assign", "override", "check", "size", "depends", "workload", "comment")


def terminal():
    """stdin and stdout read as terminals (a pytest-free stand-in for the owner typing)."""
    return mock.patch.object(human, "_ttys", return_value=True)


def session(**markers):
    """An environment with both agent markers cleared, then `markers` set: a person's terminal
    (no args) or an agent session, whichever the test names, wherever the suite runs."""
    env = {k: v for k, v in os.environ.items() if k not in MARKERS}
    env.update(markers)
    return mock.patch.dict(os.environ, env, clear=True)


class AgentSessionNeedsAuthor(unittest.TestCase):
    """An agent that omits `--author` would read as the owner: `capsule human` refuses a card
    command in an agent session without it, and the check lives in the CLI, not `human.write`."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        p = paths.board_db("b", self.home)
        db.create_board(p)
        self.conn = db.connect(p)
        self.addCleanup(self.conn.close)
        self.tid = db.new_task(self.conn, title="c")

    def files(self):
        d = spool.human_dir(self.home, "b")
        return sorted(d.glob("*.json")) if d.is_dir() else []

    def cli(self, kind, text, *extra):
        argv = ["human", "b", self.tid, kind, text, "--capsule-home", str(self.home), *extra]
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            rc = main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_agent_without_author_is_refused_with_advice_and_nothing_changes(self):
        for marker in (dict(CLAUDECODE="1"), dict(AI_AGENT="claude-code_2-1-309_agent")):
            with session(**marker):
                for kind in VERBS:
                    rc, out, err = self.cli(kind, "x")
                    self.assertNotEqual(rc, 0, (marker, kind))
                    self.assertIn("--author cos", err)
                    self.assertIn("only for the owner typing", err)    # owner is for a person
                    self.assertEqual(out, "")
        self.assertEqual(self.files(), [])
        row = self.conn.execute("SELECT workload, size_tag FROM tasks WHERE id=?",
                                (self.tid,)).fetchone()
        self.assertEqual(tuple(row), (None, None))

    def test_agent_override_without_author_is_refused_before_the_identity_check(self):
        with session(CLAUDECODE="1"):
            rc, _, err = self.cli("override", "why", "--check", "lint", "--ttl", "72h",
                                  "--fix-card", "t_fix1")
        self.assertNotEqual(rc, 0)
        self.assertIn("--author cos", err)
        self.assertEqual(self.files(), [])

    def test_person_terminal_without_author_records_owner(self):
        with session(), terminal():
            rc, out, _ = self.cli("unblock", "go")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(Path(out.strip()).read_text())["author"], "owner")

    def test_omitted_author_without_a_terminal_records_cos_not_the_owner(self):
        with session():                       # no marker, but a pipe or a script: not a person
            rc, out, _ = self.cli("unblock", "go")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(Path(out.strip()).read_text())["author"], "cos")
        with session(), terminal():
            self.assertEqual(human.cli_author(None), "owner")
        with session(CLAUDECODE="1"), terminal(), self.assertRaises(ValueError):
            human.cli_author(None)            # an agent session still must pass the flag

    def test_empty_or_zero_marker_is_not_an_agent_session(self):
        for value in ("", "0"):
            with session(CLAUDECODE=value, AI_AGENT=value):
                self.assertEqual(self.cli("unblock", "go")[0], 0, repr(value))

    def test_explicit_author_works_in_an_agent_session_and_records_what_it_says(self):
        with session(CLAUDECODE="1"):
            rc, out, _ = self.cli("unblock", "go", "--author", "cos")
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(Path(out.strip()).read_text())["author"], "cos")
            rc, out, _ = self.cli("workload", "research", "--author", "cos")
            self.assertEqual(rc, 0)

    def test_an_agent_session_cannot_record_the_owner_even_at_a_terminal(self):
        with session(CLAUDECODE="1"), terminal():
            for who in ("owner", human.LEGACY_OWNER, "Alex"):
                rc, out, err = self.cli("unblock", "go", "--author", who)
                self.assertNotEqual(rc, 0, who)
                self.assertIn("--author cos", err)
                self.assertEqual(out, "")
        self.assertEqual(self.files(), [])

    def test_explicit_owner_is_refused_without_a_terminal_and_allowed_with_one(self):
        with session():                       # no marker, but stdin/stdout are not terminals
            rc, out, err = self.cli("unblock", "go", "--author", "owner")
            self.assertNotEqual(rc, 0)
            self.assertIn("--author cos", err)
            self.assertEqual(self.files(), [])
            self.assertEqual(self.cli("unblock", "go", "--author", "cos")[0], 0)
        with session(), terminal():
            rc, out, _ = self.cli("unblock", "go", "--author", human.LEGACY_OWNER)
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(Path(out.strip()).read_text())["author"], "owner")

    def test_the_shared_writer_does_not_look_at_the_marker(self):
        """Internal routes (a listener answering from Slack) call `human.write` in-process."""
        with session(CLAUDECODE="1", AI_AGENT="x"):
            path = human.write(self.home, "b", self.tid, "unblock", "go", now=5)
        self.assertEqual(json.loads(path.read_text())["author"], "owner")

    def test_a_real_subprocess_refuses_in_an_agent_session_and_accepts_otherwise(self):
        def run(marker, *extra):
            env = {k: v for k, v in os.environ.items() if k not in MARKERS}
            env.update(marker)
            argv = [sys.executable, "-m", "capsule", "human", "b", self.tid, "unblock", "go",
                    "--capsule-home", str(self.home), *extra]
            return subprocess.run(argv, env=env, capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL)
        r = run(dict(CLAUDECODE="1"), "--author", "owner")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(run({}, "--author", "owner").returncode, 1)      # piped: not a terminal
        self.assertEqual(self.files(), [])
        r = run(dict(CLAUDECODE="1"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--author cos", r.stderr)
        self.assertEqual(self.files(), [])
        self.assertEqual(run(dict(CLAUDECODE="1"), "--author", "cos").returncode, 0)
        r = run({})
        self.assertEqual(r.returncode, 0)
        self.assertIn("recorded as cos", r.stderr)
        self.assertEqual(len(self.files()), 2)
        self.assertEqual(json.loads(Path(r.stdout.strip()).read_text())["author"], "cos")


class Q2b(unittest.TestCase):
    def numbers(self, authors):
        """Q2b numbers for one landed card with one spool file per entry (None = legacy)."""
        home, ids = trap_board(self.addCleanup, 1)
        for who in authors:
            path = human.write(home, "t", ids[0], "approve", "ok", now=AT, author=who or "owner")
            if who is None:
                doc = json.loads(path.read_text())
                del doc["author"]
                path.write_text(json.dumps(doc))
        return trap_eval(self.addCleanup, home, [])

    def test_cos_files_are_not_owner_touches(self):
        doc = self.numbers(["cos", "cos", "owner"])
        got = (doc["owner_acts"], doc["owner_touches_per_card"], doc["interventions_total"])
        self.assertEqual(got, (1, 1.0, 1))

    def test_a_legacy_file_without_author_counts_as_owner(self):
        self.assertEqual(self.numbers([None, "cos"])["owner_acts"], 1)

    def test_only_cos_files_is_a_measured_zero_not_unmeasured(self):
        doc = self.numbers(["cos"])
        self.assertEqual((doc["owner_acts"], doc["owner_touches_per_card"]), (0, 0.0))
        self.assertEqual(human.cli_author("cos"), "cos")         # a cos file stays cos
        # the legacy value reads as owner
        with terminal(), session():
            self.assertEqual(human.cli_author(human.LEGACY_OWNER), "owner")

    def test_caveat_names_the_legacy_rule_not_a_missing_field(self):
        text = " ".join(self.numbers([human.LEGACY_OWNER])["caveats"])
        self.assertIn("unless author is cos", text)
        self.assertIn("migration_resets and interventions_total exclude cos files", text)
        self.assertIn("without --author cos still counts", text)
        self.assertNotIn("no author field on human/ files", text)

    def test_cos_migration_reset_does_not_inflate_the_reset_count(self):
        home, ids = trap_board(self.addCleanup, 1)
        tag = "migration-reset"
        human.write(home, "t", ids[0], "reset", "x", now=AT, tag=tag, author="cos")
        human.write(home, "t", ids[0], "reset", "x", now=AT + 1, tag=tag)
        self.assertEqual(trap_eval(self.addCleanup, home, [])["migration_resets"], 1)


class CardFieldVerbs(unittest.TestCase):
    """R04 intent rows. Oracle 1: the sqlite column read. Oracle 2: the `capsule human` echo."""

    def setUp(self):
        from capsule import db, paths
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        p = paths.board_db("b", self.home)
        db.create_board(p)
        self.conn = db.connect(p)
        self.addCleanup(self.conn.close)
        self.parent = db.new_task(self.conn, title="p")
        self.tid = db.new_task(self.conn, title="c")

    def human(self, kind, text, *extra, rc=0):
        argv = ["human", "b", self.tid, kind, text, "--capsule-home", str(self.home), *extra]
        with session(), contextlib.redirect_stdout(io.StringIO()) as out:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(argv), rc)
        return json.loads(out.getvalue()) if rc == 0 else None

    def row(self):
        return self.conn.execute("SELECT * FROM tasks WHERE id=?", (self.tid,)).fetchone()

    def test_every_verb_round_trips_and_the_echo_matches_the_column(self):
        echo = self.human("check", "make ok", "--expect", "all green")
        row = self.row()
        self.assertEqual((row["check_cmd"], row["check_expect"]), ("make ok", "all green"))
        self.assertEqual((echo["check_cmd"], echo["check_expect"]), ("make ok", "all green"))
        echo = self.human("size", "m", "--risk-flags", "judges,data")
        row = self.row()
        flags = json.loads(row["risk_flags"])
        self.assertEqual((row["size_tag"], flags), ("m", ["data", "judges"]))
        self.assertEqual((echo["size_tag"], echo["risk_flags"]), ("m", '["data", "judges"]'))
        if DENYLIST.is_file():                        # D226: no private project is a workload
            for name in project_names():
                self.human("workload", name, rc=1)
        self.assertIsNone(self.row()["workload"])
        self.assertEqual(self.human("workload", "research")["workload"], "research")
        self.assertEqual(self.row()["workload"], "research")
        self.assertIsNone(self.row()["passes"])
        self.assertEqual(self.row()["risk_class"], "strong")      # the runner tier is never touched

    def test_bad_values_are_refused_and_write_nothing(self):
        for kind, text in (("workload", "weekend"), ("size", "has space"), ("check", "  ")):
            self.human(kind, text, rc=1)
        self.human("size", "m", "--risk-flags", "huge", rc=1)
        row = self.row()
        cols = ("workload", "size_tag", "check_cmd", "risk_flags")
        self.assertEqual([row[c] for c in cols], [None] * 4)

    def test_depends_uses_task_links(self):
        echo = self.human("depends", self.parent)
        link = self.conn.execute("SELECT parent_id, child_id FROM task_links").fetchone()
        self.assertEqual(tuple(link), (self.parent, self.tid))
        self.assertEqual(echo["depends_on"], [self.parent])
        from capsule import db
        self.assertEqual(db.open_parent(self.conn, self.tid), self.parent)
        self.human("depends", self.tid, rc=1)                      # itself

    def test_refusals_name_the_cause_and_a_reverse_dependency_is_a_cycle(self):
        self.human("depends", "t_00000000", rc=1)
        self.human("depends", self.parent)
        self.assertEqual(self.human("workload", "upkeep")["depends_on"], [self.parent])
        argv = ["human", "b", self.parent, "depends", self.tid, "--author", "cos",
                "--capsule-home", str(self.home)]
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(argv), 1)
        self.assertIn("cycle", err.getvalue())

    def test_a_three_card_loop_is_refused_and_a_repeat_link_writes_nothing(self):
        third = db.new_task(self.conn, title="g")
        self.human("depends", self.parent)                     # tid depends on parent
        argv = ["human", "b", third, "depends", self.tid, "--author", "cos",
                "--capsule-home", str(self.home)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(argv), 0)                    # third depends on tid
        links = self.conn.execute("SELECT * FROM task_links ORDER BY 1, 2").fetchall()
        events = self.conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        argv = ["human", "b", self.parent, "depends", third, "--author", "cos",
                "--capsule-home", str(self.home)]
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(argv), 1)                    # parent -> third closes the loop
        self.assertIn("cycle", err.getvalue())
        self.human("depends", self.parent)                     # already linked
        after = self.conn.execute("SELECT * FROM task_links ORDER BY 1, 2").fetchall()
        self.assertEqual(after, links)
        n_events = self.conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        self.assertEqual(n_events, events)

    def test_worker_cannot_set_check(self):
        report = {"schema": "report.v1", "run_id": "0" * 32, "task_id": self.tid, "claim_lock": "x",
                  "role": "implementer", "status": "done", "implemented": [], "left_undone": [],
                  "commands": [], "issues": [], "procedures_followed": []}
        from capsule import jsonschema, paths
        schemas = {"report.v1": jsonschema.load_schema(paths.SCHEMAS / "report.v1.json")}
        spool_dir = self.home / "spool"
        for sub in spool.SUBDIRS:
            (spool_dir / sub).mkdir(parents=True)
        (spool_dir / "report" / "1.json").write_text(json.dumps(report))
        self.assertEqual(spool.read_report(spool_dir, schemas)[1], [])      # the control is valid
        (spool_dir / "report" / "2.json").write_text(json.dumps(dict(report, check_cmd="evil")))
        doc, errs = spool.read_report(spool_dir, schemas)
        self.assertIsNone(doc)
        self.assertTrue(errs)
        from capsule import bundles
        self.conn.execute("UPDATE tasks SET passes=1, check_cmd='make ok' WHERE id=?", (self.tid,))
        prompt = bundles.implementer(dict(self.row()))["prompt"]
        self.assertIn("check_cmd: make ok", prompt)
        self.assertNotIn("passes", prompt)


_OWNER_PATCH = None


def setUpModule():
    """The decision-row text below names Alex: the matcher reads the owner name from config."""
    global _OWNER_PATCH
    _OWNER_PATCH = mock.patch.dict(os.environ, {"CAPSULE_OWNER_NAME": "Alex"})
    _OWNER_PATCH.start()


def tearDownModule():
    _OWNER_PATCH.stop()


if __name__ == "__main__":
    unittest.main()
