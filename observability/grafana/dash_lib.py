"""dash_lib.py - panel, query and threshold builders every build_dashboards dashboard uses
(split out of build_dashboards.py, card t_53c4b297; no behaviour change). build_dashboards
re-exports every name, so `from build_dashboards import gp, pgt, ...` keeps working."""
import json


PG = {"type": "grafana-postgresql-datasource", "uid": "ops-pg"}


LOKI = {"type": "loki", "uid": "ops-loki"}


def SQ(uid): return {"type": "frser-sqlite-datasource", "uid": uid}


LEVEL1 = [("Second Brain", "ops-vault")]


DRILL = [("Capsule: Postgres", "ops-capsule-postgres"),
         ("Capsule: Boards", "ops-capsule-boards")]


# ---- targets ---------------------------------------------------------------
def pgt(sql, fmt="table", refid="A"):
    return {"refId": refid, "datasource": PG, "format": fmt, "rawQuery": True,
            "editorMode": "code", "rawSql": sql}


def sqt(uid, sql, timecols=None, refid="A"):
    return {"refId": refid, "datasource": SQ(uid), "queryType": "table", "queryText": sql,
            "rawQueryText": sql, "timeColumns": timecols or []}


def lokit(expr, instant=False, legend="", refid="A"):
    t = {"refId": refid, "datasource": LOKI, "expr": expr,
         "queryType": "instant" if instant else "range"}
    if not instant:
        t["queryType"] = "range"
    if instant:
        t["instant"] = True
        t["range"] = False
    if legend:
        t["legendFormat"] = legend
    return t


def thr(steps):
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in steps]}


GREEN_ONLY = [("green", None)]


TEXT_ONLY = [("text", None)]


OUTCOME_COLORS = {
    "completed": "green", "done": "green", "review_requested": "green",
    "changes_requested": "orange", "reclaimed": "orange",
    "blocked": "blue", "running": "text",
    "timed_out": "red", "gave_up": "red", "crashed": "red",
    "spawn_failed": "red", "failed": "red", "protocol_violation": "red",
}


IDLE_MAP = [{"type": "value", "options": {
    "-": {"text": "idle", "color": "#3d3d3d", "index": 99}}}]


OUTCOME_MAP = [{"type": "value", "options": {
    k: {"color": v, "index": i} for i, (k, v) in enumerate(OUTCOME_COLORS.items())}}]


ROLE_MAP = OUTCOME_MAP + IDLE_MAP


SEVERITY_MAP = [{"type": "value", "options": {
    "error": {"color": "red", "index": 0},
    "warn": {"color": "orange", "index": 1},
    "warning": {"color": "orange", "index": 2},
    "info": {"color": "text", "index": 3}}}]


STATE_MAP = [{"type": "value", "options": {
    "ok": {"color": "green", "index": 0}, "up": {"color": "green", "index": 1},
    "failed": {"color": "red", "index": 2}, "DOWN": {"color": "red", "index": 3},
    "missing": {"color": "red", "index": 4}, "no run": {"color": "#4d4d4d", "index": 5}}}]


def gp(h, w, x, y):
    return {"h": h, "w": w, "x": x, "y": y}


def base_fc(unit=None, steps=None, mappings=None, mn=None, mx=None, links=None, decimals=None,
            noValue=None, custom=None, color=None):
    d = {"mappings": mappings or [], "thresholds": thr(steps or TEXT_ONLY)}
    if unit:
        d["unit"] = unit
    if mn is not None:
        d["min"] = mn
    if mx is not None:
        d["max"] = mx
    if links:
        d["links"] = links
    if decimals is not None:
        d["decimals"] = decimals
    if noValue:
        d["noValue"] = noValue
    if custom:
        d["custom"] = custom
    if color:
        d["color"] = color
    return {"defaults": d, "overrides": []}


def text_panel(pid, y, title, md, h=3):
    return {"id": pid, "type": "text", "title": title, "gridPos": gp(h, 24, 0, y),
            "options": {"mode": "markdown", "code": {
                "language": "plaintext", "showLineNumbers": False, "showMiniMap": False},
                        "content": md},
            "transparent": False, "fieldConfig": {"defaults": {}, "overrides": []}}


def stat(pid, title, desc, ds, targets, grid, unit=None, steps=None, mappings=None,
         links=None, graph="none", color_mode="value", text_mode="auto", calc="lastNotNull",
         decimals=None, noValue=None, values=False, fields=""):
    return {"id": pid, "title": title, "description": desc, "type": "stat", "datasource": ds,
            "targets": targets, "gridPos": grid,
            "fieldConfig": base_fc(unit=unit, steps=steps, mappings=mappings, decimals=decimals,
                                   noValue=noValue),
            "options": {"reduceOptions": {"calcs": [calc], "fields": fields,
                                      "values": values},
                        "orientation": "auto", "textMode": text_mode, "colorMode": color_mode,
                        "graphMode": graph, "justifyMode": "auto", "wideLayout": True,
                        "showPercentChange": False, "percentChangeColorMode": "standard"},
            "links": links or []}


def gauge(pid, title, desc, ds, targets, grid, unit="percent", steps=None, mn=0, mx=100,
          links=None, decimals=None):
    return {"id": pid, "title": title, "description": desc, "type": "gauge", "datasource": ds,
            "targets": targets, "gridPos": grid,
            "fieldConfig": base_fc(unit=unit, steps=steps, mn=mn, mx=mx, decimals=decimals),
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "orientation": "auto", "showThresholdLabels": False,
                        "showThresholdMarkers": True, "minVizHeight": 75, "minVizWidth": 75,
                        "sizing": "auto"},
            "links": links or []}


def bargauge(pid, title, desc, ds, targets, grid, unit="short", steps=None, mn=None, mx=None,
             values=True, display="lcd", overrides=None, orientation="horizontal"):
    fc = base_fc(unit=unit, steps=steps, mn=mn, mx=mx)
    if overrides:
        fc["overrides"] = overrides
    return {"id": pid, "title": title, "description": desc, "type": "bargauge", "datasource": ds,
            "targets": targets, "gridPos": grid, "fieldConfig": fc,
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": values},
                        "orientation": orientation, "displayMode": display, "valueMode": "color",
                        "showUnfilled": True, "minVizHeight": 16, "minVizWidth": 8,
                        "namePlacement": "auto", "sizing": "auto", "maxVizHeight": 300}}


TS_CUSTOM = {"drawStyle": "line", "lineInterpolation": "linear", "lineWidth": 2,
             "fillOpacity": 10, "gradientMode": "none", "showPoints": "auto", "pointSize": 5,
             "spanNulls": True, "axisPlacement": "auto", "axisLabel": "", "axisColorMode": "text",
             "axisBorderShow": False, "scaleDistribution": {"type": "linear"},
             "hideFrom": {"legend": False, "tooltip": False, "viz": False},
             "insertNulls": False, "barAlignment": 0, "thresholdsStyle": {"mode": "off"},
             "stacking": {"group": "A", "mode": "none"}}


def timeseries(pid, title, desc, ds, targets, grid, unit="short", stack=False, steps=None,
               mn=None, legend="bottom", draw="line", fill=10):
    custom = json.loads(json.dumps(TS_CUSTOM))
    custom["drawStyle"] = draw
    custom["fillOpacity"] = fill
    if stack:
        custom["stacking"] = {"group": "A", "mode": "normal"}
    fc = base_fc(unit=unit, steps=steps or GREEN_ONLY, mn=mn, custom=custom,
                 color={"mode": "palette-classic"})
    return {"id": pid, "title": title, "description": desc, "type": "timeseries", "datasource": ds,
            "targets": targets, "gridPos": grid, "fieldConfig": fc,
            "options": {"legend": {"displayMode": "list", "placement": legend, "showLegend": True,
                                   "calcs": []},
                        "tooltip": {"mode": "multi", "sort": "none"}}}


def barchart(pid, title, desc, ds, targets, grid, unit="short", stack=True, xfield=None,
             rotate=-45, legend="bottom", orient="auto", noValue=None):
    custom = {"lineWidth": 1, "fillOpacity": 80, "gradientMode": "none",
              "axisPlacement": "auto", "axisLabel": "", "axisColorMode": "text",
              "axisBorderShow": False, "scaleDistribution": {"type": "linear"},
              "axisCenteredZero": False,
              "hideFrom": {"legend": False, "tooltip": False, "viz": False},
              "thresholdsStyle": {"mode": "off"},
              "stacking": {"group": "A", "mode": "normal" if stack else "none"}}
    fc = base_fc(unit=unit, steps=GREEN_ONLY, custom=custom, color={"mode": "palette-classic"},
                 noValue=noValue)
    opts = {"orientation": orient, "xTickLabelRotation": rotate, "xTickLabelSpacing": 0,
            "showValue": "auto", "stacking": "normal" if stack else "none",
            "groupWidth": 0.7, "barWidth": 0.85, "barRadius": 0, "fullHighlight": False,
            "tooltip": {"mode": "multi", "sort": "none"},
            "legend": {"displayMode": "list", "placement": legend, "showLegend": True, "calcs": []}}
    if xfield:
        opts["xField"] = xfield
    return {"id": pid, "title": title, "description": desc, "type": "barchart", "datasource": ds,
            "targets": targets, "gridPos": grid, "fieldConfig": fc, "options": opts}


def timeline(pid, title, desc, ds, targets, grid, kind="state-timeline", mappings=None,
             links=None, merge=True, rowheight=0.8, noValue="no data"):
    custom = {"lineWidth": 0, "fillOpacity": 70, "spanNulls": False,
              "insertNulls": False, "hideFrom": {"legend": False, "tooltip": False, "viz": False}}
    fc = base_fc(mappings=mappings or OUTCOME_MAP, steps=TEXT_ONLY, custom=custom, links=links,
                 noValue=noValue)
    fc["defaults"]["color"] = {"mode": "thresholds"}
    opts = {"mergeValues": merge, "showValue": "auto", "alignValue": "left",
            "rowHeight": rowheight, "perPage": 20,
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "single", "sort": "none"}}
    if kind == "status-history":
        opts = {"showValue": "never", "rowHeight": rowheight, "colWidth": 0.9,
                "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                "tooltip": {"mode": "single", "sort": "none"}}
    return {"id": pid, "title": title, "description": desc, "type": kind, "datasource": ds,
            "targets": targets, "gridPos": grid, "fieldConfig": fc, "options": opts}


def table(pid, title, desc, ds, targets, grid, links=None, overrides=None, noValue=None,
          unit=None, sortby=None):
    # `noValue` is deliberately ignored: on a table it is applied per *cell*, so an empty
    # link column renders the no-data sentence in every row. An empty table already shows
    # Grafana's own "No data"; the reassuring wording lives in the description instead.
    custom = {"align": "auto", "cellOptions": {"type": "auto"}, "inspect": False,
              "filterable": False}
    fc = base_fc(unit=unit, steps=TEXT_ONLY, links=links, custom=custom)
    if overrides:
        fc["overrides"] = overrides
    return {"id": pid, "title": title, "description": desc, "type": "table", "datasource": ds,
            "targets": targets, "gridPos": grid, "fieldConfig": fc,
            "options": {"showHeader": True, "cellHeight": "sm",
                        "footer": {"show": False, "reducer": ["sum"], "countRows": False,
                                   "fields": ""},
                        "sortBy": sortby or []}}


def logs(pid, title, desc, targets, grid):
    return {"id": pid, "title": title, "description": desc, "type": "logs", "datasource": LOKI,
            "targets": targets, "gridPos": grid,
            "fieldConfig": {"defaults": {}, "overrides": []},
            "options": {"showTime": True, "showLabels": False, "showCommonLabels": False,
                        "wrapLogMessage": True, "prettifyLogMessage": False,
                        "enableLogDetails": True, "dedupStrategy": "none",
                        "sortOrder": "Descending"}}


def row(pid, title, y):
    return {"id": pid, "type": "row", "title": title, "collapsed": False, "panels": [],
            "gridPos": gp(1, 24, 0, y)}


def dashboard(uid, title, refresh, time_from, panels, templating=None, annotations=None,
              tags=None):
    ann = [{"builtIn": 1, "name": "Annotations & Alerts", "type": "dashboard", "hide": True,
            "enable": True, "iconColor": "rgba(0, 211, 255, 1)"}]
    ann += annotations or []
    links = [{"type": "link", "title": t, "url": "/d/" + u, "keepTime": True,
              "asDropdown": False, "icon": "external link", "includeVars": True,
              "targetBlank": False, "tags": [], "tooltip": ""}
             for t, u in LEVEL1 + DRILL if u != uid]
    return {
        "uid": uid, "title": title, "schemaVersion": 39, "version": 1, "editable": True,
        "graphTooltip": 0, "refresh": refresh, "tags": tags or ["capsule-corp"],
        "time": {"from": time_from, "to": "now"}, "timepicker": {}, "timezone": "browser",
        "weekStart": "", "fiscalYearStartMonth": 0, "links": links,
        "annotations": {"list": ann},
        "templating": {"list": templating or []},
        "panels": panels,
    }
