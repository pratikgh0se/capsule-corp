"""The Lookout dashboards (spec Specs/capsule-corp-lookout-design.md, notes vault, section 6, D7).

Eleven dashboards in folder "The Lookout". Each is one module `lookout_dash_<name>.py` next to this
file with UID, TITLE, FILE, a module-level SQL dict (the golden test, the live smoke and the
acceptance script read the same strings), MUST_HAVE_DATA and build(). This file holds the shared
frame and the writer, and discovers the modules by file name, so each dashboard is its own file.
Datasources: ops-pg and ops-loki only (G6). ASCII titles, a unit on every number, Asia/Bangkok
day buckets (G10)."""
import importlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_dashboards as bd  # noqa: E402
from build_dashboards import PG, data_link, gp, pgt, stat, table  # noqa: E402

OUT = HERE / "dashboards" / "lookout"
LOOKOUT_TZ = "Asia/Bangkok"
FOLDER_UID = "the-lookout"
TAGS = ["the-lookout"]
PANEL_TYPES = ("stat", "table", "bargauge", "gauge", "state-timeline", "status-history", "histogram", "barchart",
               "timeseries", "logs", "alertlist", "annolist", "text")
NAV = [("System Now", "system-now"), ("Phone summary", "phone-summary"),
       ("Factory Overview", "lookout-home"), ("Card & Epic Explorer", "lookout-explorer"),
       ("Merges & Delivery", "lookout-delivery"), ("Agent Trace & Logs", "lookout-trace"),
       ("Costs & Budgets", "lookout-costs"), ("Agent costs per card", "card-costs"),
       ("Merge timeline", "merge-timeline"), ("Incidents & alerts", "incidents"),
       ("Alerts", "lookout-alerts")]
TRACE_LINK = "/d/lookout-trace?var-product=${__data.fields.product}&var-task_id=${__data.fields.task_id}"
EXPLORER_LINK = "/d/lookout-explorer?var-product=${__data.fields.product}&var-epic=${__data.fields.epic}"
LAST_REPORT = {True: 'select max(age_s) as "age_s" from v_factory where board in ($product)',
               False: "select max(age_s) as \"age_s\" from v_factory where board = '$product'"}
LAST_REPORT_DESC = ("Seconds since the tick last reported to ops-pg (v_factory.age_s). Green under 90 s, "
                    "amber under 300 s, red beyond: a dead tick, a dead shipper or a dead Postgres.")


def modules():
    out = []
    for path in sorted(HERE.glob("lookout_dash_*.py")):
        out.append(importlib.import_module(path.stem))
    for mod in out:
        if not callable(mod.build):
            raise TypeError(f"{mod.__name__}: build must be a function returning the dashboard")
    return out


def links(self_uid):
    return [{"type": "link", "title": t, "url": "/d/" + u, "keepTime": True, "asDropdown": False,
             "icon": "dashboard", "includeVars": False, "targetBlank": False, "tags": [], "tooltip": ""}
            for t, u in NAV if u != self_uid]


def var_query(name, label, sql, multi=True, include_all=True):
    return {"name": name, "label": label, "type": "query", "datasource": PG, "query": sql, "definition": sql,
            "refresh": 2, "multi": multi, "includeAll": include_all, "hide": 0, "current": {}, "options": [],
            "sort": 1, "skipUrlSync": False, "regex": ""}


# W346: boards come from the tick heartbeat (v_factory) as well as the cards, so a board
# without shipped cards stays selectable and "All" never expands to an empty set.
PRODUCTS_SQL = "select board from v_factory union select board from cap_cards"


def var_product(multi=True):
    v = var_query("product", "Product", PRODUCTS_SQL + " order by 1", multi, multi)
    if multi:
        # I2: with no allValue, Grafana's All expands to a literal empty list on an empty
        # Lookout (no cap_cards rows yet), so every "board in ($product)" becomes the
        # Postgres syntax error "board in ()". A query allValue keeps All valid whether the
        # board list is empty or not - it re-runs the same select the variable itself uses.
        v["allValue"] = PRODUCTS_SQL
    return v


def var_epic():
    sql = ("select distinct coalesce(epic, '(none)') from cap_cards"
           " where board in ($product) order by 1")
    v = var_query("epic", "Epic", sql)
    # Same I2 guard as var_product: All must expand to a valid select, never an empty list.
    v["allValue"] = "select distinct coalesce(epic, '(none)') from cap_cards"
    return v


def var_custom(name, label, pairs, hide=0):
    """pairs: [(text, value)], the first is the default."""
    return {"name": name, "label": label, "type": "custom", "hide": hide, "multi": False, "includeAll": False,
            "query": ",".join(f"{t} : {v}" for t, v in pairs),
            "options": [{"selected": i == 0, "text": t, "value": v} for i, (t, v) in enumerate(pairs)],
            "current": {"selected": True, "text": pairs[0][0], "value": pairs[0][1]}, "skipUrlSync": False}


def var_era():
    return var_custom("era", "Era", [("capsule", "capsule"), ("hermes", "hermes")], hide=2)


def last_report(pid, multi=True):
    return stat(pid, "Last report from the factory", LAST_REPORT_DESC, PG, [pgt(LAST_REPORT[multi])],
                gp(4, 4, 20, 0), unit="s", steps=[("green", None), ("orange", 90), ("red", 300)])


def ltable(pid, title, desc, sql, grid, *, units=None, links=None, sortby=None):
    overrides = [{"matcher": {"id": "byName", "options": col}, "properties": [{"id": "unit", "value": u}]}
                 for col, u in (units or {}).items()]
    overrides += [{"matcher": {"id": "byName", "options": col},
                   "properties": [{"id": "links", "value": data_link(t, url)}]}
                  for col, (t, url) in (links or {}).items()]
    return table(pid, title, desc, PG, [pgt(sql)], grid, overrides=overrides, sortby=sortby)


def partition(panel, field):
    """One row per card on a state timeline / status history (Grafana 11.2 partitionByValues)."""
    panel["transformations"] = [{"id": "partitionByValues", "options": {"fields": [field], "keepFields": False}}]
    return panel


def frame(uid, title, panels, templating, annotations=None):
    d = bd.dashboard(uid, title, "1m", "now-24h", panels, templating=templating, annotations=annotations,
                     tags=TAGS)
    d["links"] = links(uid)
    d["timezone"] = LOOKOUT_TZ
    return d


def write_all(out_dir=None):
    out = Path(out_dir) if out_dir else Path(os.environ.get("LOOKOUT_DASHBOARDS_OUT", OUT))
    out.mkdir(parents=True, exist_ok=True)
    for m in modules():
        (out / m.FILE).write_text(json.dumps(m.build(), indent=2, ensure_ascii=True) + "\n")
        print("wrote", m.FILE)
    return out


if __name__ == "__main__":
    write_all()
