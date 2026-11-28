"""W351: the four phone-friendly Lookout views build, keep their uid and expected panels, link
from the nav, and read only real tables and views."""
import json
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "observability" / "grafana"))
import build_lookout  # noqa: E402
import lookout_dash_card_costs as costs  # noqa: E402
import lookout_dash_incidents as inc  # noqa: E402
import lookout_dash_merge_timeline as merge  # noqa: E402
import lookout_dash_phone_summary as phone  # noqa: E402

KNOWN = {"v_factory", "cap_cards", "cap_commits", "cap_runs", "cap_incidents", "now",
         "v_waiting_on_owner", "v_run_cost", "v_card_story", "live_health",
         "recent", "seg", "m", "c", "x", "r"}
EST = " (estimate at list price)"
VIEWS = {
    "phone-summary": (phone, "phone-summary.json", [
        "Factory running?", "Cards in progress", "Blocked", "Needs you", "Landed today",
        "Spend today", "Runs with no price", "Disk free", "Offline test", "What needs you"]),
    "card-costs": (costs, "card-costs.json", [
        "Where each card's money went", "Top 10 cards by tokens", "Runs with no price"]),
    "merge-timeline": (merge, "merge-timeline.json", [
        "Build, review and landing per card", "Typical time to land", "Time to land per card"]),
    "incidents": (inc, "incidents.json", [
        "Open incidents", "Came back, 30 days", "Alerts firing now", "Open incidents by type",
        "Newest open incidents"]),
}


class LookoutViews(unittest.TestCase):
    def test_each_builds_with_uid_panels_and_committed_json(self):
        for uid, (mod, fname, titles) in VIEWS.items():
            d = json.loads(json.dumps(mod.build()))
            self.assertEqual((d["uid"], mod.FILE), (uid, fname))
            have = [p["title"] for p in d["panels"]]
            for t in titles:
                self.assertIn(t, have, uid)
            self.assertEqual(json.loads((build_lookout.OUT / fname).read_text()), d, uid)

    def test_nav_links_every_view_from_every_dashboard(self):
        for uid in VIEWS:
            self.assertIn("/d/" + uid, {"/d/" + u for _, u in build_lookout.NAV})
            for path in build_lookout.OUT.glob("*.json"):
                urls = {link["url"] for link in json.loads(path.read_text())["links"]}
                self.assertEqual("/d/" + uid in urls, path.stem != uid, (uid, path.name))

    def test_phone_summary_is_one_column_of_eight_tiles(self):
        d = phone.build()
        tiles = [p for p in d["panels"] if p["type"] == "stat"]
        self.assertEqual(len(tiles), 9)
        for p in d["panels"]:
            self.assertEqual((p["gridPos"]["w"], p["gridPos"]["x"]), (24, 0), p["title"])
        self.assertEqual([p["type"] for p in d["panels"]][-1], "table")

    def test_sql_reads_only_known_tables(self):
        for uid, (mod, _, _) in VIEWS.items():
            for key, sql in mod.SQL.items():
                got = set(re.findall(r"\b(?:from|join)\s+([a-z_0-9]+)\b", sql))
                self.assertLessEqual(got, KNOWN, (uid, key))

    def test_costs_do_not_repeat_the_costs_dashboard(self):
        """Compare meaning, not SQL text: panel titles and what they group spend by."""
        import lookout_dash_costs as old
        old_titles = {re.sub(r" \(estimate at list price\)", "", p["title"]).lower()
                      for p in old.build()["panels"]}
        new_panels = costs.build()["panels"]
        for p in new_panels:
            title = re.sub(r" \(estimate at list price\)", "", p["title"]).lower()
            self.assertNotIn(title, old_titles, p["title"])
        # Same grouping of spend (by role, by model, by product, ranked cards) is a repeat.
        for p in new_panels:
            for t in p.get("targets", []):
                sql = t["rawSql"].lower()
                if "usd_est" in sql and p["type"] in ("bargauge", "barchart", "piechart"):
                    self.fail("spend ranking repeats Costs & Budgets: " + p["title"])
                if "group by 1" in sql and "role" in sql and "usd_est" in sql:
                    self.fail("spend by role repeats Costs & Budgets: " + p["title"])
        self.assertEqual(costs.build()["time"]["from"], "now-7d")

    def test_unpriced_runs_are_surfaced(self):
        for mod in (costs, phone):
            self.assertIn("usd_est is null", mod.SQL["unpriced"])
        self.assertIn("'unknown'", phone.SQL["offline"])
        self.assertNotIn("else 'FAILING'", phone.SQL["offline"])

    def test_card_links_use_a_real_task_id_column(self):
        self.assertIn("as task_id", costs.SQL["table"])
        self.assertIn("as task_id", merge.SQL["table"])

if __name__ == "__main__":
    unittest.main()
