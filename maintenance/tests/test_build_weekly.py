"""OBS-12: the Weekly health dashboard builds, stays on ops-pg, reads only tables that exist in
init.sql / the migrations / the samples table, and follows the Lookout variable rules.
Oracle 1: the schema files. Oracle 2: the committed JSON (regenerate == committed). The
deploy-time live EXPLAIN against real ops-pg is a host step."""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "observability" / "grafana"))
sys.path.insert(0, str(REPO / "maintenance"))
import build_weekly  # noqa: E402
import obs_probe_ship  # noqa: E402

PG_DIR = REPO / "observability" / "postgres"
FILES = [PG_DIR / "init.sql", *sorted((PG_DIR / "migrations").glob("*.sql"))]
SCHEMA = re.sub(r"--[^\n]*", "", "\n".join(p.read_text() for p in FILES))
KNOWN = set(re.findall(r"create (?:or replace )?(?:table|view)(?: if not exists)? ([a-z_]+)",
                       SCHEMA, re.I))


def walk(node, key):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                yield v
            yield from walk(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v, key)


class WeeklyHealth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = json.loads(json.dumps(build_weekly.build()))

    def test_builds_with_the_expected_panels(self):
        self.assertEqual(self.d["uid"], "weekly-health")
        self.assertEqual(self.d["timezone"], "Asia/Bangkok")
        titles = [p["title"] for p in self.d["panels"]]
        for t in ("Cards delivered per week", "First-pass review rate", "Median lead time per card",
                  "Incidents opened and repeats", "Interventions per delivered card",
                  "Fresh tokens per delivered card", "Disk free"):
            self.assertIn(t, titles)
        self.assertTrue(all(t.isascii() for t in titles))

    def test_only_allowed_datasources(self):
        uids = {v.get("uid") for v in walk(self.d, "datasource") if isinstance(v, dict)}
        self.assertEqual(uids, {"ops-pg"})

    def test_sql_reads_only_known_tables(self):
        for key, sql in build_weekly.SQL.items():
            sql = sql.replace("extract(epoch from", "extract(epoch,")
            tables = set(re.findall(r"\b(?:from|join)\s+([a-z_]+)\b", sql))
            self.assertLessEqual(tables, KNOWN | {"samples_hourly"}, key)
            self.assertNotIn("in ()", sql)

    def test_columns_exist_in_the_schema_files(self):
        """Z2 second oracle: every column the SQL reads is declared by its table's DDL."""
        used = {"cap_cards": ("phase", "completed_at", "created_at", "review_rounds", "board",
                              "task_id"),
                "cap_runs": ("board", "task_id", "era", "tokens_in", "tokens_out",
                             "tokens_cache_write"),
                "cap_incidents": ("opened_at", "repeat_of", "kind"),
                "cap_epics": ("publish_state", "publish_reason"),
                "events": ("ts", "kind", "payload"),
                "samples_hourly": ("bucket", "name", "avg_value", "total_avg", "n")}
        for table, cols in used.items():
            # scoped: only queries that read this table (the summary embeds all the others)
            sql = " ".join(q for k, q in build_weekly.SQL.items() if k != "summary" and
                           re.search(r"\b(?:from|join)\s+" + table + r"\b", q))
            self.assertTrue(sql, table)
            m = re.search(r"create table (?:if not exists )?" + table + r" \((.*?)\n?\)\s*;",
                          SCHEMA, re.I | re.S)
            self.assertTrue(m, table)
            declared = set(re.findall(r"(?:^|,)\s*([a-z_]+) ", m.group(1)))
            for col in cols:
                self.assertIn(col, declared, (table, col))
                self.assertRegex(sql, r"\b" + col + r"\b")
        kinds = (REPO / "capsule" / "event_kinds.py").read_text()
        self.assertRegex(kinds, r'"intervention": \([^)]*"tag"[^)]*"author"')
        self.assertIn("denied_tools", kinds)

    def test_long_range_panels_read_the_hourly_rollup_not_raw_samples(self):
        for key in ("disk", "load", "cpu", "mem"):
            sql = build_weekly.SQL[key]
            self.assertIn("from samples_hourly where", sql, key)
            self.assertNotRegex(sql, r"\bsamples\b(?!_)", key)

    def test_container_panels_read_the_per_tick_total_not_the_per_container_mean(self):
        """One tick, two containers: the panel must show their sum (old code showed the mean)."""
        import sqlite3
        for key in ("cpu", "mem"):
            sql = build_weekly.SQL[key]
            self.assertIn("total_avg", sql, key)
            self.assertNotIn("sum(avg_value)", sql, key)
        # run the rollup's SELECT (sqlite spelling of the hour and no time filter) on 2 containers
        sel = re.search(r"(SELECT date_trunc.*?) ON CONFLICT", obs_probe_ship.ROLLUP_SQL).group(1)
        sel = re.sub(r"WHERE .*? GROUP BY", "GROUP BY", sel, count=1)
        sel = sel.replace("date_trunc('hour', ts)", "strftime('%Y-%m-%d %H', ts)")
        db = sqlite3.connect(":memory:")
        db.execute("create table samples (ts text, service text, name text, value real,"
                   " labels text)")
        db.executemany("insert into samples values ('2026-11-02 10:00:00', 'capsule', 'c', ?, ?)",
                       [(30.0, "a"), (50.0, "b")])
        (avg, mn, mx, n, total), = db.execute(
            "select avg_value, min_value, max_value,"  # noqa: S608  (constants only)
            " n, total_avg from (" + sel.replace(
                "sum(s) / sum(c), min(mn), max(mx), sum(c), avg(s)",
                "sum(s) / sum(c) avg_value, min(mn) min_value, max(mx) max_value, sum(c) n,"
                " avg(s) total_avg") + ")").fetchall()
        self.assertEqual((avg, mn, mx, n, total), (40.0, 30.0, 50.0, 2, 80.0))

    def test_container_hours_weigh_by_ticks_when_the_container_count_changes(self):
        """Hour A has 2 containers, hour B has 3: the mean per-tick total is not row-weighted."""
        import sqlite3
        db = sqlite3.connect(":memory:")
        db.execute("create table samples_hourly (bucket text, total_avg real, n int, ticks int)")
        # A: 10 ticks of 2 rows each, total 20 per tick. B: 10 ticks of 3 rows, total 60 per tick.
        db.executemany("insert into samples_hourly values (?, ?, ?, ?)",
                       [("A", 20.0, 20, 10), ("B", 60.0, 30, 10)])
        sql = build_weekly.SQL["cpu"]
        inner = re.search(r"from \((select bucket as ts.*?)\) s group by 1", sql, re.S).group(1)
        inner = re.sub(r"where name = .*? group by bucket", "group by bucket", inner)
        agg = re.search(r"round\(\((sum\(.*?\))\)::numeric", sql).group(1)
        query = "select " + agg + " from (" + inner + ")"  # noqa: S608  (constants only)
        (v,), = db.execute(query).fetchall()
        self.assertEqual(v, 40.0)

    def test_samples_names_are_documented(self):
        doc = (REPO / "docs" / "observability-conventions.md").read_text()
        for name in re.findall(r"name = '(capsule\.[a-z0-9_.]+)'", json.dumps(self.d)):
            self.assertIn(name, doc)

    def test_no_quoted_multi_value_variables_and_no_dollar_in_all_value(self):
        for v in self.d["templating"]["list"]:
            self.assertNotIn("$", v.get("allValue", ""), v["name"])
        multi = [v["name"] for v in self.d["templating"]["list"] if v.get("multi")]
        for sql in walk(self.d["panels"], "rawSql"):
            for name in multi:
                self.assertNotIn("'$" + name + "'", sql)

    def test_numeric_panels_have_a_unit(self):
        for p in self.d["panels"]:
            if p["type"] in ("stat", "timeseries"):
                self.assertTrue(p["fieldConfig"]["defaults"].get("unit"), p["title"])

    def _security_titles(self):
        panels, out, inside = self.d["panels"], [], False
        for p in sorted(panels, key=lambda p: (p["gridPos"]["y"], p["gridPos"]["x"])):
            if p["type"] == "row":
                inside = p["title"] == "Security"
            elif inside:
                out.append(p["title"])
        return out

    def test_security_group_holds_exactly_its_panels(self):
        self.assertEqual([p["title"] for p in self.d["panels"] if p["type"] == "row"],
                         ["Security"])
        self.assertEqual(self._security_titles(),
                         ["Tool denials", "Override grants", "Override refusals",
                          "Overrides expired", "Security incidents"])

    def test_override_queries_filter_on_the_event_name(self):
        for key, event in (("grants", "override"), ("refusals", "override_refused"),
                           ("expired", "override")):
            sql = build_weekly.SQL[key]
            self.assertIn("from events where kind = 'card'", sql)
            self.assertIn("payload->>'event' = '" + event + "'", sql)
            fields = set(re.findall(r"payload\s*->>?\s*'([a-z_]+)'", sql))
            self.assertEqual(fields, {"event", "expires_at"} if key == "expired" else {"event"})

    def test_overrides_expired_buckets_grants_by_their_expiry_epoch(self):
        sql = build_weekly.SQL["expired"]
        self.assertNotIn("override_expired", sql)
        self.assertIn("to_timestamp((payload->>'expires_at')::float) at time zone", sql)
        self.assertIn("jsonb_typeof(payload->'expires_at') = 'number'", sql)
        self.assertIn("> now() - interval '8 weeks'", sql)
        lookout = (REPO / "capsule" / "lookout.py").read_text()
        self.assertIn('{"expires_at": p.get("expires_at")}', lookout)

    def test_security_kinds_equal_the_invariant_kinds(self):
        sys.path.insert(0, str(REPO))
        from capsule import invariants
        self.assertEqual(sorted(build_weekly.SECURITY_KINDS), sorted(invariants.KINDS))
        self.assertNotIn("capsule", build_weekly.__dict__)

    def test_security_incidents_use_the_named_constant(self):
        self.assertEqual(set(build_weekly.SECURITY_KINDS),
                         {"main_checkout_write", "two_dispatchers", "lost_card_state"})
        self.assertEqual(len(build_weekly.SECURITY_KINDS), 3)
        sql = build_weekly.SQL["security_inc"]
        self.assertIn("from cap_incidents where kind in ('main_checkout_write', "
                      "'two_dispatchers', 'lost_card_state') and", sql)
        self.assertNotIn("payload", sql)
        self.assertNotIn("root_cause", sql)

    def test_override_events_exist_in_event_kinds(self):
        kinds = (REPO / "capsule" / "event_kinds.py").read_text()
        for event in ("override", "override_refused", "override_expired"):
            self.assertIn('"' + event + '": (', kinds)

    def test_build_is_deterministic(self):
        self.assertEqual(json.dumps(build_weekly.build()), json.dumps(build_weekly.build()))

    def test_docstring_panel_count_matches(self):
        n = int(re.search(r"(\d+) panels", build_weekly.__doc__).group(1))
        self.assertEqual(n, len([p for p in self.d["panels"] if p["type"] != "row"]))
        ids = [p["id"] for p in self.d["panels"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_regenerate_matches_committed_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            got = build_weekly.write(tmp).read_bytes()
        self.assertEqual(got, (build_weekly.OUT / build_weekly.FILE).read_bytes())


if __name__ == "__main__":
    unittest.main()
