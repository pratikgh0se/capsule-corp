#!/usr/bin/env python3
"""lookout_smoke.py - run every Lookout panel and variable query against ops-pg (spec 10.4).

Reads the generated JSON in observability/grafana/dashboards/lookout/, substitutes the Grafana
macros and variables, runs each query read-only and prints PASS / FAIL / EMPTY / SKIP per panel.
Fails (exit 1) on any SQL error and on an empty result for a "must have data" panel."""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from capsule import paths  # noqa: E402

DASH = Path(__file__).resolve().parents[1] / "observability" / "grafana" / "dashboards" / "lookout"
MUST_HAVE = {"Factory", "Last report from the factory", "Budget windows", "Prices assumed",
             "Every card"}
TIME_FILTER = re.compile(r"\$__timeFilter\(([^)]*)\)")
PG = "grafana-postgresql-datasource"


def _quote(v):
    return "'" + str(v).replace("'", "''") + "'"


def substitute(sql, variables):
    """variables: name -> (values, multi, is_expression). Time range is fixed at 24 h."""
    sql = TIME_FILTER.sub(r"\1 between now() - interval '24 hours' and now()", sql)
    for name in sorted(variables, key=len, reverse=True):
        values, multi, expr = variables[name]
        pat = r"\$\{?" + name + r"\}?(?![A-Za-z0-9_])"
        first = values[0] if values else ""
        if expr:  # custom variable whose value is an SQL expression (e.g. $since)
            sql = re.sub(pat, lambda m: first, sql)
            continue
        sql = re.sub(r"'" + pat + "'", lambda m: _quote(first), sql)
        every = ",".join(_quote(v) for v in values) if multi and values else _quote(first)
        sql = re.sub(pat, lambda m: every, sql)
    return sql


def _psql(sql):
    """Run one query through `docker exec psql` on the ops postgres, forced read-only."""
    import events
    body = "set default_transaction_read_only = on;\n" + sql.strip().rstrip(";") + ";\n"
    argv = ["exec", "-i", paths.PG_CONTAINER, "psql", "-U", "ops", "-d", "ops",
            "-q", "-t", "-A", "-F", "|", "-v", "ON_ERROR_STOP=1", "-f", "-"]
    r = events.docker_run(argv, input=body, text=True, capture_output=True, timeout=60)
    if r.returncode != 0:
        lines = (r.stderr or r.stdout).strip().splitlines() or [f"psql exit {r.returncode}"]
        raise RuntimeError(lines[-1][:200])
    return [ln.split("|")[0] for ln in r.stdout.splitlines() if ln.strip()]


def _variables(dash, run, report):
    """Resolve the template variables; custom ones have no dependencies, so they go first
    (costs uses $era before it is listed)."""
    out, uid = {}, dash["uid"]
    listed = dash.get("templating", {}).get("list", [])
    for var in sorted(listed, key=lambda v: v["type"] != "custom"):
        name = var["name"]
        if var["type"] == "custom":
            cur = (var.get("current") or {}).get("value")
            first = (var.get("query") or "").split(",")[0].split(" : ")[-1].strip()
            out[name] = ([str(cur) if cur is not None else first], False, name == "since")
            continue
        try:
            values = run(substitute(var["query"], out))
        except RuntimeError as exc:
            report("FAIL", uid, "variable", name, str(exc))
            values = []
        out[name] = (values, bool(var.get("multi")), False)
    return out


def _panel(p, uid, variables, run, report):
    ds = p.get("datasource")
    targets = [t for t in p.get("targets", []) if t.get("rawSql")]
    if not targets or (isinstance(ds, dict) and ds.get("type") != PG):
        return report("SKIP", uid, "panel", p["title"], "not a Postgres query")
    for t in targets:
        try:
            rows = run(substitute(t["rawSql"], variables))
        except RuntimeError as exc:
            report("FAIL", uid, "panel", p["title"], str(exc))
            continue
        if rows:
            report("PASS", uid, "panel", p["title"], f"{len(rows)} rows")
        elif p["title"] in MUST_HAVE:
            report("FAIL", uid, "panel", p["title"], "empty, must have data")
        else:
            report("EMPTY", uid, "panel", p["title"], "no rows (allowed)")


def main(argv=None, run=None, dash_dir=None):
    """`run(sql) -> list of first-column strings`, raising RuntimeError on a SQL error."""
    run = run or _psql
    files = sorted(Path(dash_dir or DASH).glob("*.json"))
    if not files:
        print("lookout_smoke: no dashboard JSON found", file=sys.stderr)
        return 2
    fails = []

    def report(status, uid, kind, title, detail):
        fails.extend([1] * (status == "FAIL"))
        print(f"{status:5} {uid} {kind} {title!r}: {detail}")

    for f in files:
        dash = json.loads(f.read_text())
        variables = _variables(dash, run, report)
        todo = list(dash["panels"])
        while todo:
            p = todo.pop(0)
            todo += p.get("panels", [])
            _panel(p, dash["uid"], variables, run, report)
    print(f"lookout_smoke: {len(fails)} failing")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
