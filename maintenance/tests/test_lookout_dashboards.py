"""OBS-05: all six Lookout dashboards build, the nav bar has no dead link, datasources stay inside
ops-pg / ops-loki (alert lists use Grafana's own alerting) and the costs dashboard reads only the
cost views. Oracle 1: the NAV list in build_lookout.py. Oracle 2: the committed JSON files."""
import json
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "observability" / "grafana"))
import build_lookout  # noqa: E402

DATASOURCES = {"ops-pg", "ops-loki"}
GRAFANA_OWN = {"alertlist", "annolist"}
COST_TABLES = {"v_run_cost", "prices", "events"}


def mods():
    return {m.UID: m for m in build_lookout.modules()}


def datasource_uids(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "datasource" and isinstance(v, dict):
                yield v.get("uid")
            yield from datasource_uids(v)
    elif isinstance(node, list):
        for v in node:
            yield from datasource_uids(v)


def raw_sql(node):
    if isinstance(node, dict):
        if isinstance(node.get("rawSql"), str):
            yield node["rawSql"]
        for v in node.values():
            yield from raw_sql(v)
    elif isinstance(node, list):
        for v in node:
            yield from raw_sql(v)


class SixDashboards(unittest.TestCase):
    def test_each_dashboard_builds(self):
        self.assertEqual(set(mods()), {u for _, u in build_lookout.NAV})
        for uid, m in mods().items():
            d = json.loads(json.dumps(m.build()))
            self.assertEqual(d["uid"], uid)
            self.assertTrue(d["panels"], uid)

    def test_nav_resolves_to_six_json_files(self):
        self.assertEqual(len(build_lookout.NAV), 11)
        for title, uid in build_lookout.NAV:
            path = build_lookout.OUT / mods()[uid].FILE
            self.assertTrue(path.is_file(), (title, path))
            self.assertEqual(json.loads(path.read_text())["uid"], uid)
        self.assertEqual(len(list(build_lookout.OUT.glob("*.json"))), 11)

    def test_datasources_are_ops_pg_or_ops_loki(self):
        for uid, m in mods().items():
            d = m.build()
            for p in d["panels"]:
                if p["type"] in GRAFANA_OWN:
                    self.assertNotIn("datasource", p, (uid, p["title"]))
                    self.assertIn(uid, ("lookout-alerts", "incidents"))
            self.assertLessEqual(set(datasource_uids(d)), DATASOURCES, uid)

    def test_costs_sql_reads_only_cost_tables(self):
        sql = mods()["lookout-costs"].SQL
        self.assertTrue(sql)
        for key, text in sql.items():
            tables = set(re.findall(r"\b(?:from|join)\s+([a-z_]+)\b", text))
            self.assertTrue(tables, key)
            self.assertLessEqual(tables, COST_TABLES, key)

    def test_multi_value_variables_are_not_quoted_and_all_values_have_no_vars(self):
        for uid, m in mods().items():
            d = m.build()
            multi = [v["name"] for v in d["templating"]["list"] if v.get("multi")]
            for v in d["templating"]["list"]:
                self.assertNotIn("$", v.get("allValue", ""), (uid, v["name"]))
            texts = list(raw_sql(d["panels"]))
            texts += [v["query"] for v in d["templating"]["list"] if v["type"] == "query"]
            for text in texts:
                for name in multi:
                    self.assertNotIn("'$" + name + "'", text, (uid, name))


if __name__ == "__main__":
    unittest.main()
