"""Task 6 (The Lookout): golden JSON + structural rules on every generated Lookout panel (spec 10.3).
Oracle 1: regenerate == committed bytes. Oracle 2: the deploy-time live query of every panel
(maintenance/lookout_smoke.py, Task 7) against real ops-pg."""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "observability" / "grafana"))
import build_lookout  # noqa: E402

A8 = {"stat", "table", "bargauge", "gauge", "state-timeline", "status-history", "histogram", "barchart",
      "timeseries", "logs", "alertlist", "annolist", "text"}
NUMERIC = {"stat", "gauge", "bargauge", "timeseries", "barchart", "histogram"}
OWN_UIDS = {"system-now.json": "system-now", "phone-summary.json": "phone-summary",
            "card-costs.json": "card-costs", "merge-timeline.json": "merge-timeline",
            "incidents.json": "incidents"}     # W347, W351: not "lookout-<file>"
NO_PICKERS = set(OWN_UIDS.values())     # own test files; no product picker, no last-report tile
CARD_DASHBOARDS = {"lookout-home", "lookout-explorer", "lookout-delivery"}
KINDS = set(re.findall(r"\('([a-z_]+)', '", (REPO / "observability" / "postgres" / "migrations" /
                                              "003-lookout.sql").read_text()))
SUFFIX_UNITS = {"_s": "s", "_usd": "currencyUSD", "_h": "h", "_pct": "percent"}


def generated():
    with tempfile.TemporaryDirectory() as tmp:
        out = build_lookout.write_all(out_dir=tmp)
        return {p.name: json.loads(p.read_text()) for p in sorted(Path(out).glob("*.json"))}


def panels(dash):
    for p in dash["panels"]:
        yield p
        yield from p.get("panels", [])


def sqls(panel):
    return [t["rawSql"] for t in panel.get("targets", []) if t.get("rawSql")]


class Golden(unittest.TestCase):
    def test_regenerate_matches_committed_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = build_lookout.write_all(out_dir=tmp)
            got = {p.name: p.read_bytes() for p in Path(out).glob("*.json")}
        want = {p.name: p.read_bytes() for p in build_lookout.OUT.glob("*.json")}
        self.assertEqual(set(got), set(want))
        for name in want:
            self.assertEqual(got[name], want[name], f"{name} differs from build_lookout.py's output")

    def test_uids_and_files_match_the_spec(self):
        spec = dict(build_lookout.NAV)
        self.assertLessEqual(set(OWN_UIDS), set(generated()))
        for name, d in generated().items():
            self.assertIn(d["uid"], spec.values())
            want = OWN_UIDS.get(name, "lookout-" + name[:-5])
            self.assertEqual(d["uid"], want)


class Rules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dash = generated()

    def each(self):
        for name, d in self.dash.items():
            for p in panels(d):
                if p["type"] != "row":
                    yield d, p

    def test_panel_types_exist_in_grafana_11_2(self):
        for d, p in self.each():
            self.assertIn(p["type"], A8, (d["uid"], p["title"]))

    def test_numeric_panels_have_a_unit(self):
        for d, p in self.each():
            if p["type"] in NUMERIC:
                self.assertTrue(p["fieldConfig"]["defaults"].get("unit"), (d["uid"], p["title"]))

    def test_table_aliases_with_a_unit_suffix_carry_that_unit(self):
        for d, p in self.each():
            if p["type"] != "table":
                continue
            units = {o["matcher"]["options"]: pr["value"] for o in p["fieldConfig"]["overrides"]
                     for pr in o["properties"] if pr["id"] == "unit"}
            for sql in sqls(p):
                for alias in re.findall(r'\bas "?([a-z_]+)"?', sql):
                    for suf, unit in SUFFIX_UNITS.items():
                        if alias.endswith(suf):
                            self.assertEqual(units.get(alias), unit, (d["uid"], p["title"], alias))

    def test_titles_are_operator_language_and_ascii(self):
        for d, p in self.each():
            t = p.get("title", "")
            self.assertNotIn("_", t, (d["uid"], t))
            self.assertFalse({w for w in re.findall(r"[a-z_]+", t.lower())} & {k for k in KINDS if "_" in k},
                             (d["uid"], t))
            self.assertTrue(t.isascii() and (p.get("description") or "").isascii(), (d["uid"], t))
        for d in self.dash.values():
            self.assertTrue(d["title"].isascii())

    def test_no_sqlite_datasource_anywhere(self):
        self.assertNotIn("frser", json.dumps(self.dash))

    def test_card_tables_link_to_trace(self):
        """M3 (fix round 2, tightened): a per-row `${__data.fields.link}` data link only counts
        as "links to Trace" when the query reads the waiting-on-owner view, the one view whose
        `link` column is known to route card rows to /d/lookout-trace (and epic/PR rows to their own
        target - the reason a blanket TRACE_LINK is wrong there, M3 round 1). Every other card
        table must carry the literal /d/lookout-trace link - a `${__data.fields.link}` override
        from an unrelated view (no `link` column, or one that points elsewhere, e.g. GitHub)
        must not satisfy this rule."""
        for d, p in self.each():
            if d["uid"] in CARD_DASHBOARDS and p["type"] == "table" and any("task_id" in s for s in sqls(p)):
                fc = json.dumps(p["fieldConfig"])
                if any("from v_waiting_on_owner" in s for s in sqls(p)):
                    self.assertIn("${__data.fields.link}", fc, (d["uid"], p["title"]))
                else:
                    self.assertIn("/d/lookout-trace", fc, (d["uid"], p["title"]))

    def test_daily_buckets_use_lookout_tz(self):
        for d, p in self.each():
            for sql in sqls(p):
                for arg in re.findall(r"date_trunc\('day', ([^()]*)\)", sql):
                    self.assertIn("at time zone 'Asia/Bangkok'", arg, (d["uid"], p["title"]))

    def test_run_queries_filter_era(self):
        for d, p in self.each():
            for sql in sqls(p):
                if "cap_runs" in sql or "v_run_cost" in sql:
                    self.assertRegex(sql, r"era = '\$era'", (d["uid"], p["title"]))

    def test_health_panels_stay_inside_14_days(self):
        for d, p in self.each():
            for sql in sqls(p):
                if "kind = 'health'" in sql:
                    days = [int(n) for n in re.findall(r"interval '(\d+) days'", sql)]
                    self.assertTrue(days and max(days) <= 14, (d["uid"], p["title"]))

    def test_every_dashboard_has_last_report_nav_and_product(self):
        self.assertLessEqual(NO_PICKERS, {d["uid"] for d in self.dash.values()})
        for d in self.dash.values():
            titles = [p["title"] for p in panels(d)]
            urls = {"/d/" + u for _, u in build_lookout.NAV} - {"/d/" + d["uid"]}
            self.assertEqual({l["url"] for l in d["links"]}, urls)
            if d["uid"] in NO_PICKERS:     # W347, W351: no product picker (own test files)
                continue
            self.assertIn("Last report from the factory", titles, d["uid"])
            names = [v["name"] for v in d["templating"]["list"]]
            self.assertIn("product", names)
            self.assertIn("era", names)
            self.assertEqual(d["time"]["from"], "now-24h")
            self.assertEqual(d["timezone"], "Asia/Bangkok")

    def test_every_panel_sql_comes_from_its_module_sql_dict(self):
        mods = {m.UID: m for m in build_lookout.modules()}
        shared = set(build_lookout.LAST_REPORT.values())
        for d, p in self.each():
            for sql in sqls(p):
                self.assertIn(sql, set(mods[d["uid"]].SQL.values()) | shared, (d["uid"], p["title"]))
            for t in p.get("targets", []):
                if t.get("expr"):
                    self.assertIn(t["expr"], getattr(mods[d["uid"]], "LOKI", {}).values())

    def test_run_id_variables_have_no_catch_all_default(self):
        """I1: a multi run_id variable with allValue='.*' (or '.+') defaults to every run on
        every board when nothing is selected yet, or when the card has no runs. Grafana's own
        '(r1|r2|...)' All-value expansion is the safe default."""
        for d in self.dash.values():
            for v in d["templating"]["list"]:
                if v["name"] == "run_id":
                    self.assertNotIn(v.get("allValue"), (".*", ".+"), (d["uid"], v))

    def test_multi_product_variable_all_value_is_a_query(self):
        """I2: a multi, include-all product variable with no allValue expands All to an empty
        list on an empty Lookout, so every 'board in ($product)' becomes 'board in ()' - a
        Postgres syntax error. allValue must itself be a query, so All always resolves to
        something valid, empty or not."""
        for d in self.dash.values():
            for v in d["templating"]["list"]:
                if v["name"] == "product" and v.get("multi"):
                    self.assertTrue(v.get("allValue", "").strip().lower().startswith("select"),
                                    (d["uid"], v))

    def test_no_sql_contains_an_empty_in_clause(self):
        """I2 regression trip-wire: no generated SQL literally contains 'in ()', which is a
        Postgres syntax error the empty-Lookout case must never reach."""
        for d, p in self.each():
            for sql in sqls(p):
                self.assertNotIn("in ()", sql, (d["uid"], p["title"]))

    def test_task_scoped_log_queries_use_line_filter_with_single_value_picker(self):
        """M4 (fix round 2, corrected): tick.log and outbox.log are each one file covering
        every card - fluent-bit (observability/fluent-bit/labels.lua, capsule/lookout-t5-loki
        7687b1d) sets task_id as a label only on the per-card worker log path, never on these,
        so a task_id label match here selects nothing for a real card (round 1's fix was
        backwards). The `|= "$task_id"` line filter is the only way to find a card in a shared
        file. What keeps an unset $task_id from matching every line is the task_id variable
        itself: single-value, no All option, no allValue, so Grafana always resolves it to a
        real task_id whenever a card exists - the empty case happens only on an empty board,
        where there is nothing to show anyway."""
        for m in build_lookout.modules():
            for key, expr in getattr(m, "LOKI", {}).items():
                if key in ("tick_log", "outbox_log"):
                    self.assertIn('|= "$task_id"', expr, (m.UID, key))
                    self.assertNotIn("task_id=\"$task_id\"", expr, (m.UID, key))   # no label matcher
        for d in self.dash.values():
            for v in d["templating"]["list"]:
                if v["name"] == "task_id":
                    self.assertFalse(v.get("multi"), (d["uid"], v))
                    self.assertFalse(v.get("includeAll"), (d["uid"], v))
                    self.assertNotIn("allValue", v, (d["uid"], v))


class Overview(unittest.TestCase):
    def setUp(self):
        import lookout_dash_home as home
        self.m, self.d = home, home.build()
        self.titles = [p["title"] for p in panels(self.d)]

    def test_panels_answer_q1_to_q9(self):
        for t in ("Factory", "Working now", "Waiting on you", "Shipped since you looked", "Budget windows",
                  "Open incidents", "On the floor now", "Waiting on you (what to do)",
                  "Finished since you looked", "PRs in flight", "Who worked on what"):
            self.assertIn(t, self.titles)

    def test_floor_reads_v_floor_with_product_epic_role_model_elapsed(self):
        sql = self.m.SQL["floor"]
        self.assertIn("from v_floor", sql)
        for col in ("product", "epic", "task_id", "role", "model", "elapsed_s", "repo"):
            self.assertIn(col, sql)

    def test_since_variable_defaults_to_the_morning_brief(self):
        since = [v for v in self.d["templating"]["list"] if v["name"] == "since"][0]
        self.assertEqual(since["current"]["value"], "(select since_ts from v_since)")

    def test_no_alert_list_on_home(self):
        self.assertNotIn("alertlist", [p["type"] for p in panels(self.d)])      # D2


class Trace(unittest.TestCase):
    def test_trace_variables_and_panels(self):
        import lookout_dash_trace as trace
        d = trace.build()
        names = {v["name"]: v for v in d["templating"]["list"]}
        self.assertIn("select task_id from cap_cards where board = '$product' order by snap_ts desc",
                      names["task_id"]["query"])
        self.assertTrue(names["run_id"]["multi"])
        self.assertNotIn("allValue", names["run_id"])          # I1: no catch-all default
        self.assertEqual(trace.LOKI["worker_log"],
                         '{job="capsule-worker", board="$product", run_id=~"$run_id"}')
        titles = [p["title"] for p in panels(d)]
        for t in ("This card", "Runs", "Timeline", "What happened", "Worker log", "Tick log mentions",
                  "Messages sent", "Incidents on this card"):
            self.assertIn(t, titles)
        self.assertIn("from v_card_story", trace.SQL["story"])


if __name__ == "__main__":
    unittest.main()
