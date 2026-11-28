"""W347: the System Now dashboard builds, has its uid, refresh, 8 sections and the nav link."""
import json
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "observability" / "grafana"))
import build_lookout  # noqa: E402
import lookout_dash_system_now as sn  # noqa: E402

ALLOWED = {"live_health", "live_containers", "live_seats", "v_floor", "cap_cards",
           "cap_commits", "lanes", "m", "now", "lateral"}


def _step(panel, i):
    return panel["fieldConfig"]["defaults"]["thresholds"]["steps"][i]["value"]


class SystemNow(unittest.TestCase):
    def setUp(self):
        self.d = json.loads(json.dumps(sn.build()))

    def test_uid_refresh_and_folder_file(self):
        got = (self.d["uid"], self.d["title"], self.d["refresh"])
        self.assertEqual(got, ("system-now", "System Now", "30s"))
        committed = json.loads((build_lookout.OUT / "system-now.json").read_text())
        self.assertEqual(committed, self.d)

    def test_eight_sections_in_order(self):
        rows = [p for p in self.d["panels"] if p["type"] == "row"]
        self.assertEqual(len(rows), 8)
        self.assertEqual([r["title"].split(".")[0] for r in rows], [str(i) for i in range(1, 9)])
        ys = [r["gridPos"]["y"] for r in rows]
        self.assertEqual(ys, sorted(ys))

    def test_every_other_dashboard_links_here_and_it_links_back(self):
        self.assertIn(("System Now", "system-now"), build_lookout.NAV)
        for path in build_lookout.OUT.glob("*.json"):
            urls = {link["url"] for link in json.loads(path.read_text())["links"]}
            self.assertEqual("/d/system-now" in urls, path.name != "system-now.json", path.name)

    def test_plain_english_and_ops_pg_only(self):
        for p in self.d["panels"]:
            self.assertNotIn("_", p["title"])
            if p["type"] != "row":
                self.assertEqual(p["datasource"]["uid"], "ops-pg")
        by_title = {p["title"]: p for p in self.d["panels"]}
        self.assertEqual(_step(by_title["Disk free"], 1), 25)
        self.assertEqual(_step(by_title["Snapshot age"], 1), 180)

    def test_sql_reads_only_known_sources(self):
        for key, sql in sn.SQL.items():
            tables = set(re.findall(r"\b(?:from|join)\s+([a-z_]+)\b", sql))
            self.assertLessEqual(tables, ALLOWED, key)


if __name__ == "__main__":
    unittest.main()
