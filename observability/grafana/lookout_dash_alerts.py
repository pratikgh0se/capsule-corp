"""Alerts (uid lookout-alerts; spec 6.7): the only place alert state lives (D2). The alert list
panels read Grafana's own alerting, not ops-pg; the rule inputs panel reads the usage meter."""
import re

from build_dashboards import PG, gp, pgt, text_panel, timeseries
from build_lookout import FOLDER_UID, HERE, frame, last_report, var_era, var_product

UID, TITLE, FILE = "lookout-alerts", "Alerts", "alerts.json"
SQL = {
    "inputs": ("select ts as time, (payload->>'session_pct')::float as \"5-hour window\","
               " (payload->>'week_pct')::float as \"weekly window\" from events"
               " where kind = 'usage' and $__timeFilter(ts) order by 1"),
}
MUST_HAVE_DATA = ()
MUTE_FILE = HERE / "provisioning" / "alerting" / "mute-timings.yaml"
MUTE_HEAD = ["Muted notices come from `mute-timings.yaml`.", ""]
ANNO = {"onlyFromThisDashboard": False, "onlyInTimeRange": True, "tags": [], "limit": 20,
        "showUser": False, "showTime": True, "showTags": True, "navigateToPanel": False,
        "navigateBefore": "10m", "navigateAfter": "10m"}
STATES = {"firing": True, "pending": True, "noData": False, "normal": False, "error": True}


def alertlist(pid, title, desc, grid, states):
    return {"id": pid, "type": "alertlist", "title": title, "description": desc, "gridPos": grid,
            "options": {"viewMode": "list", "groupMode": "default", "groupBy": [], "maxItems": 20,
                        "sortOrder": 1, "dashboardAlerts": False, "alertName": "",
                        "showInstances": False, "alertInstanceLabelFilter": "",
                        "folder": {"uid": FOLDER_UID, "title": "The Lookout"},
                        "stateFilter": states},
            "fieldConfig": {"defaults": {}, "overrides": []}}


def build():
    all_states = {k: True for k in STATES}
    history = {"id": 4, "type": "annolist", "title": "Alert history", "gridPos": gp(8, 24, 0, 12),
               "description": "When rules fired and cleared, from Grafana's alert state history.",
               "options": ANNO, "fieldConfig": {"defaults": {}, "overrides": []}}
    text = MUTE_FILE.read_text()
    names = re.findall(r"^\s+name:\s*(\S+)", text, re.M)
    spans = re.findall(r'start_time:\s*"?([\d:]+)"?\s+end_time:\s*"?([\d:]+)"?', text)
    muted = [f"- {n}: {a} to {b}" for n, (a, b) in zip(names, spans)] or ["- no mute timings"]
    p = [
        last_report(1),
        alertlist(2, "Firing now", "What is wrong right now (Q1): firing and pending rules.",
                  gp(8, 12, 0, 4), STATES),
        alertlist(3, "All rules", "Every rule being watched, in any state.", gp(8, 12, 12, 4),
                  all_states),
        history,
        timeseries(5, "Rule inputs", "What the window rules look at: the 5-hour and weekly"
                   " usage percent.", PG, [pgt(SQL["inputs"], fmt="time_series")],
                   gp(8, 16, 0, 20), unit="percent", mn=0),
        text_panel(6, 20, "Muting", "\n".join(MUTE_HEAD + muted), h=8),
    ]
    p[-1]["gridPos"] = gp(8, 8, 16, 20)
    return frame(UID, TITLE, p, [var_product(), var_era()])
