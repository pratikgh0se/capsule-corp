#!/usr/bin/env python3
"""db_migrate.py - apply the numbered SQL files under observability/postgres/migrations/
to the live Postgres, once each."""
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO / "observability" / "postgres" / "migrations"

sys.path.insert(0, str(Path(__file__).resolve().parent))
_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from capsule import paths  # noqa: E402
import events   # for DOCKER_BIN / docker_env - one constant, honouring
                # AGENT_OPS_DOCKER_BIN, instead of a second hardcoded literal.

LEDGER_SQL = ("CREATE TABLE IF NOT EXISTS schema_migrations ("
              "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());")


def _default_psql(sql):
    """(returncode, stdout). Same door events._psql_copy and cleanup.sh use."""
    try:
        r = events.docker_run(["exec", "-i", paths.PG_CONTAINER, "psql", "-U", "ops", "-d", "ops",
                               "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1", "-f", "-"],
                              input=sql, text=True, capture_output=True, timeout=120)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as exc:
        return 1, str(exc)


PROBE_SQL = "select to_regclass('schema_migrations');"


def _read_ledger(psql):
    rc, out = psql("select name from schema_migrations;")
    if rc != 0:
        raise RuntimeError(f"db_migrate: could not read schema_migrations: {out.strip()[:200]}")
    return {line.strip() for line in out.splitlines() if line.strip()}


def applied(psql=None):
    """Names already in schema_migrations. Creates the ledger first, so a first
    run on a live table works. WRITES - apply mode only; check/list mode uses
    `applied_or_none`."""
    psql = psql or _default_psql
    rc, out = psql(LEDGER_SQL)
    if rc != 0:
        raise RuntimeError(f"db_migrate: could not create schema_migrations: {out.strip()[:200]}")
    return _read_ledger(psql)


def applied_or_none(psql=None):
    """Same answer as `applied()`, but read-only: `to_regclass` returns NULL for a
    relation that does not exist, which means "no ledger, so nothing is applied yet"
    - never "create it"."""
    psql = psql or _default_psql
    rc, out = psql(PROBE_SQL)
    if rc != 0:
        raise RuntimeError(f"db_migrate: could not probe schema_migrations: {out.strip()[:200]}")
    if not out.strip():          # NULL -> the ledger is not there
        return set()
    return _read_ledger(psql)


def pending(applied_names, migrations_dir=None):
    root = Path(migrations_dir or MIGRATIONS_DIR)
    return [p for p in sorted(root.glob("*.sql")) if p.name not in applied_names]


def _script(path):
    """One transaction: the migration, then its ledger row. ON CONFLICT DO
    NOTHING so a re-run of a migration whose row already exists still commits."""
    return ("BEGIN;\n"
            + path.read_text(encoding="utf-8").rstrip() + "\n"
            + f"INSERT INTO schema_migrations (name) VALUES ('{path.name}') "
              "ON CONFLICT (name) DO NOTHING;\n"
              "COMMIT;")


def apply(migrations_dir=None, psql=None):
    psql = psql or _default_psql
    todo = pending(applied(psql), migrations_dir)
    done = []
    for path in todo:
        rc, out = psql(_script(path))
        if rc != 0:
            raise RuntimeError(f"db_migrate: {path.name} failed: {out.strip()[:400]}")
        print(f"db_migrate: applied {path.name}")
        done.append(path.name)
    if not done:
        print("db_migrate: nothing pending")
    return done


def main(argv=None, psql=None, migrations_dir=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if os.environ.get("AGENT_OPS_NO_DOCKER"):
        print("db_migrate: skipped (AGENT_OPS_NO_DOCKER=1)")
        return 0
    try:
        if "--check" in argv or "--list" in argv:
            todo = pending(applied_or_none(psql), migrations_dir)
            for path in todo:
                print(f"db_migrate: pending {path.name}")
            if "--list" in argv:
                return 0
            return 1 if todo else 0
        apply(migrations_dir, psql)
        return 0
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
