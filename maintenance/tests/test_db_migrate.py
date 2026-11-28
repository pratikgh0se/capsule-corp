import os, sys, tempfile, unittest, unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["AGENT_OPS_NO_SEND"] = "1"
os.environ.setdefault("EVENTS_SPOOL", str(Path(tempfile.mkdtemp(prefix="events-mig-")) / "events.jsonl"))
import db_migrate


class FakePsql:
    """A psql that remembers the scripts it ran and answers the ledger query."""

    def __init__(self, applied=(), fail_on=None, has_ledger=True):
        self.applied = list(applied)
        self.fail_on = fail_on
        # `to_regclass` answers NULL for a table that is not there. Default
        # True: most cases model a database that has already been migrated
        # once. LEDGER_SQL flips it, the way CREATE TABLE would.
        self.has_ledger = has_ledger
        self.scripts = []

    def __call__(self, sql):
        self.scripts.append(sql)
        if self.fail_on and self.fail_on in sql:
            return 1, "ERROR: syntax error"
        if "to_regclass" in sql:
            return 0, ("schema_migrations\n" if self.has_ledger else "\n")
        if "create table if not exists schema_migrations" in sql.lower():
            self.has_ledger = True
            return 0, ""
        if "select name from schema_migrations" in sql.lower():
            return 0, "\n".join(self.applied) + ("\n" if self.applied else "")
        for name in list(self.applied):
            pass
        return 0, ""


def _migrations(*names):
    root = Path(tempfile.mkdtemp(prefix="migrations-"))
    for n in names:
        (root / n).write_text(f"-- {n}\nALTER TABLE events ADD COLUMN IF NOT EXISTS x int;\n")
    return root


class Pending(unittest.TestCase):
    def test_pending_is_sorted_and_excludes_applied(self):
        root = _migrations("002-b.sql", "001-a.sql", "010-c.sql")
        self.assertEqual([p.name for p in db_migrate.pending(set(), root)],
                          ["001-a.sql", "002-b.sql", "010-c.sql"])
        self.assertEqual([p.name for p in db_migrate.pending({"001-a.sql"}, root)],
                          ["002-b.sql", "010-c.sql"])

    def test_non_sql_files_are_ignored(self):
        root = _migrations("001-a.sql")
        (root / "README.md").write_text("notes")
        self.assertEqual([p.name for p in db_migrate.pending(set(), root)], ["001-a.sql"])

    def test_the_shipped_migration_is_present_and_named_for_sorting(self):
        names = [p.name for p in sorted(db_migrate.MIGRATIONS_DIR.glob("*.sql"))]
        self.assertIn("001-event-envelope.sql", names)
        for n in names:
            self.assertRegex(n, r"^\d{3}-[a-z0-9-]+\.sql$")


class Apply(unittest.TestCase):
    def test_it_creates_the_ledger_before_reading_it(self):
        p = FakePsql()
        db_migrate.apply(_migrations("001-a.sql"), psql=p)
        self.assertIn("create table if not exists schema_migrations", p.scripts[0].lower())

    def test_each_migration_runs_inside_one_transaction_with_its_ledger_row(self):
        p = FakePsql()
        done = db_migrate.apply(_migrations("001-a.sql", "002-b.sql"), psql=p)
        self.assertEqual(done, ["001-a.sql", "002-b.sql"])
        body = p.scripts[-1]
        self.assertTrue(body.startswith("BEGIN;"))
        self.assertIn("ALTER TABLE events ADD COLUMN IF NOT EXISTS x int;", body)
        self.assertIn("INSERT INTO schema_migrations (name) VALUES ('002-b.sql')", body)
        self.assertIn("ON CONFLICT (name) DO NOTHING", body)
        self.assertTrue(body.rstrip().endswith("COMMIT;"))

    def test_an_already_applied_migration_is_not_re_run(self):
        p = FakePsql(applied=["001-a.sql"])
        self.assertEqual(db_migrate.apply(_migrations("001-a.sql"), psql=p), [])
        self.assertEqual(len([s for s in p.scripts if "BEGIN;" in s]), 0)

    def test_a_failing_migration_stops_the_run_and_raises(self):
        p = FakePsql(fail_on="002-b.sql")
        with self.assertRaises(RuntimeError) as ctx:
            db_migrate.apply(_migrations("001-a.sql", "002-b.sql"), psql=p)
        self.assertIn("002-b.sql", str(ctx.exception))

    def test_apply_is_a_no_op_the_second_time(self):
        root = _migrations("001-a.sql")
        p = FakePsql()

        def psql(sql):
            rc, out = p(sql)
            if "BEGIN;" in sql:
                p.applied.append(sql.split("VALUES ('")[1].split("'")[0])
            return rc, out

        self.assertEqual(db_migrate.apply(root, psql=psql), ["001-a.sql"])
        self.assertEqual(db_migrate.apply(root, psql=psql), [])


class DockerBin(unittest.TestCase):
    """Minor 3, promoted because it is load-bearing for test safety: this
    module used to hardcode Docker.app's bin dir, so `_default_psql` reached
    the REAL docker whatever a test put on PATH - and therefore the live
    capsule-corp-postgres. It now shares events.docker_env(), which honours
    AGENT_OPS_DOCKER_BIN (the same override deploy.sh:288 uses)."""

    def test_the_docker_path_literal_lives_in_events_only(self):
        import events
        self.assertIs(db_migrate.events, events)
        src = Path(db_migrate.__file__).read_text(encoding="utf-8")
        self.assertNotIn("/Applications/Docker.app", src)

    def test_agent_ops_docker_bin_overrides_the_default(self):
        import events
        with unittest.mock.patch.dict(os.environ, {"AGENT_OPS_DOCKER_BIN": "/fake/bin"}):
            self.assertEqual(events.docker_bin(), "/fake/bin")
            self.assertTrue(events.docker_env(guard=False)["PATH"].startswith("/fake/bin:"))

    def test_the_default_is_used_when_the_override_is_absent(self):
        import events
        env = dict(os.environ)
        env.pop("AGENT_OPS_DOCKER_BIN", None)
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(events.docker_bin(), events.DOCKER_BIN)

    def test_default_psql_runs_docker_through_the_overridden_path(self):
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"], seen["env"] = argv, kw.get("env")

            class R:
                returncode, stdout, stderr = 0, "", ""
            return R()

        stub = tempfile.mkdtemp()
        (Path(stub) / "docker").write_text("#!/bin/sh\nexit 0\n")
        (Path(stub) / "docker").chmod(0o755)
        with unittest.mock.patch.dict(os.environ, {"AGENT_OPS_DOCKER_BIN": stub}), \
             unittest.mock.patch("subprocess.run", fake_run):
            db_migrate._default_psql("select 1;")
        self.assertEqual(seen["argv"][:3], ["docker", "exec", "-i"])
        self.assertTrue(seen["env"]["PATH"].startswith(stub + ":"))


class Cli(unittest.TestCase):
    # main() unconditionally skips under AGENT_OPS_NO_DOCKER even when a fake
    # `psql` is injected (it's a CLI-wide safety switch, not test-only), so
    # every test here but the one that means to exercise that switch must
    # not inherit it from the ambient environment (CI sets it for the shell
    # tests later in verify.yml; a run with that var already exported, or a
    # developer's own shell, must not spill into these) - the same class of
    # host/CI-environment dependency as the worker-image pin (2026-09-09).
    def setUp(self):
        self._no_docker = os.environ.pop("AGENT_OPS_NO_DOCKER", None)

    def tearDown(self):
        if self._no_docker is None:
            os.environ.pop("AGENT_OPS_NO_DOCKER", None)
        else:
            os.environ["AGENT_OPS_NO_DOCKER"] = self._no_docker

    def test_no_docker_skips_and_exits_zero(self):
        os.environ["AGENT_OPS_NO_DOCKER"] = "1"
        self.assertEqual(db_migrate.main([]), 0)
        self.assertEqual(db_migrate.main(["--check"]), 0)

    def test_check_exits_1_when_something_is_pending_and_writes_nothing(self):
        p = FakePsql()
        self.assertEqual(db_migrate.main(["--check"], psql=p, migrations_dir=_migrations("001-a.sql")), 1)
        self.assertEqual([s for s in p.scripts if "BEGIN;" in s], [])

    def test_check_exits_0_when_everything_is_applied(self):
        p = FakePsql(applied=["001-a.sql"])
        self.assertEqual(db_migrate.main(["--check"], psql=p, migrations_dir=_migrations("001-a.sql")), 0)

    # --- I3: check/list mode must issue no DDL at all -----------------------
    # `applied()` always ran LEDGER_SQL, so `deploy.sh --check` (deploy.sh:299)
    # CREATEd schema_migrations on a database it promised to only read
    # ("--check/--dry-run must never touch the network or write", deploy.sh:314).

    def test_check_never_issues_ddl(self):
        p = FakePsql()
        db_migrate.main(["--check"], psql=p, migrations_dir=_migrations("001-a.sql"))
        joined = " ".join(p.scripts).lower()
        for ddl in ("create table", "create index", "insert", "alter table", "begin;"):
            self.assertNotIn(ddl, joined)

    def test_list_never_issues_ddl(self):
        p = FakePsql()
        db_migrate.main(["--list"], psql=p, migrations_dir=_migrations("001-a.sql"))
        joined = " ".join(p.scripts).lower()
        for ddl in ("create table", "create index", "insert", "alter table", "begin;"):
            self.assertNotIn(ddl, joined)

    def test_check_probes_the_ledger_with_to_regclass(self):
        p = FakePsql()
        db_migrate.main(["--check"], psql=p, migrations_dir=_migrations("001-a.sql"))
        self.assertIn("to_regclass('schema_migrations')", " ".join(p.scripts))

    def test_an_absent_ledger_means_every_migration_is_pending(self):
        # `to_regclass` on a missing relation returns NULL, which psql -t -A
        # prints as an empty line: read that as "nothing applied yet", not as
        # a reason to create the table.
        class NoLedger(FakePsql):
            def __call__(self, sql):
                self.scripts.append(sql)
                if "to_regclass" in sql:
                    return 0, "\n"
                raise AssertionError(f"check mode must not run {sql!r}")

        p = NoLedger()
        self.assertEqual(db_migrate.main(["--check"], psql=p,
                                         migrations_dir=_migrations("001-a.sql", "002-b.sql")), 1)
        self.assertEqual(len(p.scripts), 1)

    def test_a_present_ledger_is_still_read_in_check_mode(self):
        class WithLedger(FakePsql):
            def __call__(self, sql):
                self.scripts.append(sql)
                if "to_regclass" in sql:
                    return 0, "schema_migrations\n"
                if "select name from schema_migrations" in sql.lower():
                    return 0, "001-a.sql\n"
                raise AssertionError(f"check mode must not run {sql!r}")

        p = WithLedger()
        self.assertEqual(db_migrate.main(["--check"], psql=p,
                                         migrations_dir=_migrations("001-a.sql")), 0)

    def test_apply_still_creates_the_ledger(self):
        p = FakePsql()
        db_migrate.apply(_migrations("001-a.sql"), psql=p)
        self.assertIn("create table if not exists schema_migrations", p.scripts[0].lower())

    def test_a_probe_failure_is_a_hard_error_not_an_empty_ledger(self):
        class Broken(FakePsql):
            def __call__(self, sql):
                self.scripts.append(sql)
                return 2, "could not connect to server"

        with self.assertRaises(RuntimeError):
            db_migrate.applied_or_none(psql=Broken())


if __name__ == "__main__":
    unittest.main()
