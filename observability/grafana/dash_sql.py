"""dash_sql.py - shared variables, SQL fragments and link/override helpers for the
build_dashboards dashboards (split out of build_dashboards.py, card t_53c4b297; no behaviour
change). build_dashboards re-exports every name."""
from dash_lib import SEVERITY_MAP


ROLES = ["dev", "dev-frontend", "dev-backend", "infra", "qa", "lead", "researcher"]


JOBS = ["morning", "nightly", "report", "inbox", "weekly", "security", "agent-review",
        "sprint-plan", "cleanup", "sprint-sync", "observability"]


VAULT_JOBS = ["morning", "nightly", "report", "inbox", "weekly", "security", "agent-review",
              "cleanup"]


USD_NOTE = "List-price estimate; you are on a subscription, so this is a weight, not a bill."


# ---------------------------------------------------------------- SQL fragments
def role_pivot(alias_from, window_sql):
    cols = ",\n".join(
        "  coalesce(max(case when t.assignee='%s' then coalesce(r.outcome, r.status) end),"
        " '-') as \"%s\"" % (r, r)
        for r in ROLES)
    idle = ", ".join(["'-'"] * (len(ROLES) + 1))
    return ("select * from (\n"
            "select r.started_at as time,\n%s,\n"
            "  coalesce(max(case when t.assignee is null or t.assignee not in (%s)"
            " then coalesce(r.outcome, r.status) end), '-') as \"other\"\n"
            "from task_runs r left join tasks t on t.id = r.task_id\n"
            "where r.started_at >= %s\ngroup by r.started_at\n"
            "union all select cast(%s as integer), %s\n"
            "union all select cast(strftime('%%s','now') as integer), %s\n"
            ") order by 1;"
            % (cols, ",".join("'%s'" % r for r in ROLES), window_sql, window_sql, idle, idle))


NEEDS_ME_COUNT_SQL = (
    "with opens as (\n"
    "  select coalesce(nullif(task_id,''), nullif(slug,'')) as key, ts\n"
    "  from events where kind='card' and payload->>'state'='open'),\n"
    "closes as (\n"
    "  select coalesce(nullif(task_id,''), nullif(slug,'')) as key, max(ts) as ts\n"
    "  from events where kind='card' and payload->>'state'='closed' group by 1)\n"
    "select count(*) as open_decisions from opens o\n"
    "left join closes c on c.key = o.key and c.ts > o.ts where c.key is null")


AI_LATEST_ROWS = (
    "jsonb_array_elements(coalesce(e.payload->'hermes','[]'::jsonb)"
    " || coalesce(e.payload->'claude_code','[]'::jsonb)) x")


AI_LATEST_WHERE = ("where e.kind='ai_usage'"
                   " and e.ts = (select max(ts) from events where kind='ai_usage')")


TOKENS_24H_SQL = ("select sum((x->>'input')::bigint + (x->>'output')::bigint\n"
                  "  + (x->>'cache_read')::bigint + (x->>'cache_write')::bigint)"
                  " as \"tokens, 24 h\"\n"
                  "from events e, " + AI_LATEST_ROWS + "\n" + AI_LATEST_WHERE)


TOKENS_24H_DESC = ("All tokens (input + output + cache) over every row of the latest hourly "
                   "ai_usage sample, which covers a rolling 24 h window - this is the tile to "
                   "read for today, since the per-day series excludes the current day.")


TODAY_EXCLUDED = ("Today is excluded: the current day is still filling, so its bar would "
                  "always read low. The Last 24 h tile next to it covers today.")


def ai_daily(days, expr, extra_group=""):
    # `ts < date_trunc('day', now())`: whole days only.
    return (
        "with latest as (\n"
        "  select distinct on (date_trunc('day', ts)) ts, payload from events\n"
        "  where kind='ai_usage' and ts > now() - interval '%d days'\n"
        "    and ts < date_trunc('day', now())\n"
        "  order by date_trunc('day', ts), ts desc)\n"
        "select date_trunc('day', e.ts) as time%s, %s\n"
        "from latest e, %s\ngroup by 1%s order by 1" % (
            days, extra_group and ", " + extra_group.split(" as ")[-1] or "", expr,
            AI_LATEST_ROWS, extra_group and ", 2" or ""))


def data_link(title, url):
    return [{"title": title, "url": url, "targetBlank": False}]


def severity_override(field="severity"):
    """Colour the severity cell by its value: error red, warn amber, info neutral."""
    return [{"matcher": {"id": "byName", "options": field},
             "properties": [{"id": "custom.cellOptions", "value": {"type": "color-text"}},
                            {"id": "mappings", "value": SEVERITY_MAP}]}]


def url_cell_override(field="link"):
    return [{"matcher": {"id": "byName", "options": field},
             "properties": [{"id": "links", "value": [
                 {"title": "Open the link", "url": "${__value.raw}", "targetBlank": True}]}]}]
