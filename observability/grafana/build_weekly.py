"""Weekly health dashboard (OBS-12; the owner approved 2026-10-23): 18 panels, this week against
last week, from tables ops-pg already has, plus a Security row (the row is not a panel).
Built with the Lookout helpers but kept out of
lookout_dash_*.py on purpose: that glob is the Lookout's six-dashboard set (NAV, golden tests,
"exactly six files"), and this change must not touch those dashboards.
Datasource: ops-pg only. Weeks start Monday, Asia/Bangkok. Week to date is compared with the last
full week, so early-week deltas look low. Run `python3 build_weekly.py` to write the JSON."""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from build_lookout import PG, frame, ltable  # noqa: E402
from build_dashboards import gp, pgt, row, stat, timeseries  # noqa: E402

UID, TITLE, FILE = "weekly-health", "Weekly health", "weekly-health.json"
OUT = HERE / "dashboards" / "weekly"
FRESH = "coalesce(r.tokens_in, 0) + coalesce(r.tokens_out, 0) + coalesce(r.tokens_cache_write, 0)"
SPAN = "interval '8 weeks'"


def wk(col):
    return ("(date_trunc('week', " + col + " at time zone 'Asia/Bangkok')"
            " at time zone 'Asia/Bangkok')")


def done(extra=""):
    return ("from cap_cards where phase = 'done' and completed_at is not null"
            " and completed_at > now() - " + SPAN + extra)


DELIVERED = ("select " + wk("completed_at") + " as time, count(*)::float as v " + done() +
             " group by 1")
INTERVENTIONS = ("select " + wk("ts") + " as time, count(*)::float as v from events"
                 " where kind = 'card' and payload->>'event' = 'intervention'"
                 " and coalesce(payload->>'author', '') <> 'cos'"
                 " and coalesce(payload->>'tag', '') not in ('migration-reset', 'handover-route')"
                 " and ts > now() - " + SPAN + " group by 1")
FRESH_SUM = ("select " + wk("c.completed_at") + " as time, sum(" + FRESH + ")::float as v"
             " from cap_runs r join cap_cards c on c.board = r.board and c.task_id = r.task_id"
             " where c.phase = 'done' and c.completed_at > now() - " + SPAN +
             " and r.era = 'capsule' group by 1")
INC = ("select " + wk("opened_at") + " as time, count(*)::float as opened,"
       " count(repeat_of)::float as repeats from cap_incidents"
       " where opened_at > now() - " + SPAN + " group by 1")
DENIED = ("select " + wk("ts") + " as time, count(*)::float as v from events"
          " where kind = 'card' and payload->>'event' = 'crashed'"
          " and jsonb_typeof(payload->'denied_tools') = 'array'"
          " and jsonb_array_length(payload->'denied_tools') > 0"
          " and ts > now() - " + SPAN + " group by 1")
SECURITY_KINDS = ("main_checkout_write", "two_dispatchers", "lost_card_state")
SECURITY_INC = ("select " + wk("opened_at") + " as time, count(*)::float as v from cap_incidents"
                " where kind in (" + ", ".join("'" + k + "'" for k in SECURITY_KINDS) + ")"
                " and opened_at > now() - " + SPAN + " group by 1")


def override_count(event):
    """Weekly count of one override event; reads only the event name from the payload."""
    return ("select " + wk("ts") + " as time, count(*)::float as v from events"
            " where kind = 'card' and payload->>'event' = '" + event + "'"
            " and ts > now() - " + SPAN + " group by 1")


LAPSE = "to_timestamp((payload->>'expires_at')::float)"
# override_expired is written only when a lapsed grant is re-judged, so count grants by the week
# their own expires_at (an epoch) falls in instead.
EXPIRED = ("select " + wk(LAPSE) + " as time, count(*)::float as v from events"
           " where kind = 'card' and payload->>'event' = 'override'"
           " and jsonb_typeof(payload->'expires_at') = 'number'"
           " and " + LAPSE + " > now() - " + SPAN + " group by 1")


def per_card(num):
    return ("select d.time, round((coalesce(n.v, 0) / nullif(d.v, 0))::numeric, 2)::float as v"
            " from (" + DELIVERED + ") d left join (" + num + ") n on n.time = d.time")


# D255: the 8-week panels read the hourly rollup (samples_hourly, one row per series per hour),
# never the raw samples, which keep 90 days. Hours combine as a mean weighted by n (rows per hour).
def wavg(expr, w="n"):
    return "sum((" + expr + ") * " + w + ") / sum(" + w + ")"


def sample(name, expr):
    return ("select " + wk("ts") + " as time, "  # noqa: S608  (name is a literal at each call)
            + expr + "::float as v from (select bucket as ts,"
            " avg_value as value, n from samples_hourly where name = '" + name + "' and bucket >"
            " now() - " + SPAN + ") s group by 1")


# Containers: one probe row per container per tick, so the panel shows the per-tick total across
# containers (total_avg), summed over services per hour; hours weigh by ticks, not rows.
def summed(name, expr):
    return ("select " + wk("ts") + " as time, "  # noqa: S608  (name is a literal at each call)
            + expr + "::float as v from (select bucket as ts,"
            " sum(total_avg) as value, max(ticks) as ticks from samples_hourly where name = '" +
            name + "' and bucket > now() - " + SPAN + " group by bucket) s group by 1")


# metric label -> (weekly select returning time, v; count-like so a missing week is 0)
METRICS = {
    "Cards delivered": (DELIVERED, True),
    "First-pass review rate (%)": (
        "select " + wk("completed_at") + " as time,"
        " round((100.0 * avg((review_rounds <= 1)::int))::numeric, 1)::float as v " + done() +
        " group by 1", False),
    "Median lead time per card (hours)": (
        "select " + wk("completed_at") + " as time, round((percentile_cont(0.5) within group"
        " (order by extract(epoch from completed_at - created_at) / 3600))::numeric, 1)::float"
        " as v " + done(" and created_at is not null") + " group by 1", False),
    "Incidents opened": ("select time, opened as v from (" + INC + ") i", True),
    "Repeat incidents": ("select time, repeats as v from (" + INC + ") i", True),
    "Interventions per card": (per_card(INTERVENTIONS), False),
    "Fresh tokens per delivered card": (per_card(FRESH_SUM), False),
    "Disk free (%)": (
        sample("capsule.host.disk.pct", "round((" + wavg("100 - value") + ")::numeric, 1)"),
        False),
    "Host load, 1 minute": (
        sample("capsule.host.load1", "round((" + wavg("value") + ")::numeric, 2)"), False),
    "Containers CPU (%)": (
        summed("capsule.container.cpu.pct",
               "round((" + wavg("value", "ticks") + ")::numeric, 1)"), False),
    "Containers memory (GB)": (
        summed("capsule.container.mem.bytes",
               "round(((" + wavg("value", "ticks") + ") / 1e9)::numeric, 2)"), False),
    "Tool denials": (DENIED, True),
}
ORDER = {name: i for i, name in enumerate(METRICS)}


NOW_WK = "(date_trunc('week', now() at time zone 'Asia/Bangkok') at time zone 'Asia/Bangkok')"
PICK = "max(v) filter (where time = %s)"
PARTS = [
    "select %d as ord, '%s' as metric, %s as this_week, %s as last_week from (%s) w" % (
        ORDER[name], name,
        "coalesce(" * zero + PICK % NOW_WK + ", 0)" * zero,
        "coalesce(" * zero + PICK % (NOW_WK[:-1] + " - interval '1 week')") + ", 0)" * zero,
        sql)
    for name, (sql, zero) in METRICS.items()]
SUMMARY = ("select metric, this_week as \"this week\", last_week as \"last week\","
           " round((100.0 * (this_week - last_week) / nullif(last_week, 0))::numeric, 0)"
           " as change_pct from (" + " union all ".join(PARTS) + ") s order by ord")


FLIP = [("green", None), ("orange", 1)]
REFUSED = ("select count(*)::float as \"epics\" from cap_epics where publish_state = 'refused'"
           " and publish_reason = '%s'")
SQL = {
    "summary": SUMMARY,
    "delivered": DELIVERED,
    "first_pass": METRICS["First-pass review rate (%)"][0],
    "lead": METRICS["Median lead time per card (hours)"][0],
    "incidents": INC,
    "interventions": METRICS["Interventions per card"][0],
    "fresh": METRICS["Fresh tokens per delivered card"][0],
    "disk": METRICS["Disk free (%)"][0],
    "load": METRICS["Host load, 1 minute"][0],
    "cpu": METRICS["Containers CPU (%)"][0],
    "mem": METRICS["Containers memory (GB)"][0],
    "denied": DENIED,
    "grants": override_count("override"),
    "refusals": override_count("override_refused"),
    "expired": EXPIRED,
    "security_inc": SECURITY_INC,
    "secret_refused": REFUSED % "secret_found",
    "auth_refused": REFUSED % "gh_auth",
}


def bars(pid, title, desc, key, grid, unit="short"):
    return timeseries(pid, title, desc, PG, [pgt(SQL[key], fmt="time_series")], grid, unit=unit,
                      draw="bars", fill=80)


def build():
    p = [
        ltable(1, "This week against last week", "Week to date (Monday, Bangkok time) against the"
               " last full week, so early in the week the change reads low. A blank cell means no"
               " samples that week. Source: the Lookout tables and the host samples.",
               SQL["summary"], gp(13, 24, 0, 0), units={"change_pct": "percent"}),
        bars(2, "Cards delivered per week", "Cards that reached done, by the week they completed.",
             "delivered", gp(7, 8, 0, 13)),
        bars(3, "First-pass review rate", "Share of delivered cards that needed one implementer"
             " round (review_rounds of 1 or less).", "first_pass", gp(7, 8, 8, 13), unit="percent"),
        bars(4, "Median lead time per card", "Created to completed, median of the cards delivered"
             " that week.", "lead", gp(7, 8, 16, 13), unit="h"),
        bars(5, "Incidents opened and repeats", "Opened per week; repeats reopen the same"
             " fingerprint within 30 days of a close (repeat_of is set).", "incidents",
             gp(7, 8, 0, 20)),
        bars(6, "Interventions per delivered card", "Human step-ins (intervention events) divided"
             " by cards delivered in the same week.", "interventions", gp(7, 8, 8, 20)),
        bars(7, "Fresh tokens per delivered card", "Input, output and cache-write tokens of every"
             " run on cards delivered that week, divided by cards delivered.", "fresh",
             gp(7, 8, 16, 20)),
        bars(8, "Disk free", "Weekly average of free space on the factory volume.", "disk",
             gp(7, 6, 0, 27), unit="percent"),
        bars(9, "Host load (1 minute)", "Weekly average load.", "load", gp(7, 6, 6, 27)),
        bars(10, "Containers CPU", "Weekly average of all containers' CPU, summed.", "cpu",
             gp(7, 6, 12, 27), unit="percent"),
        bars(11, "Containers memory", "Weekly average of all containers' memory, summed.", "mem",
             gp(7, 6, 18, 27), unit="decgbytes"),
        stat(13, "Epics refused: secret found", "Right now, epics whose PR the publisher refused"
             " because a secret was found. A snapshot, not a weekly history.", PG,
             [pgt(SQL["secret_refused"])], gp(3, 6, 0, 34), unit="short", steps=FLIP),
        stat(14, "Epics refused: GitHub login", "Right now, epics refused because gh is not"
             " logged in. A snapshot, not a weekly history.", PG, [pgt(SQL["auth_refused"])],
             gp(3, 6, 6, 34), unit="short", steps=FLIP),
        row(15, "Security", 37),
        bars(12, "Tool denials", "Runs that crashed after a tool call was denied"
             " (the agent guard). The only denial record in ops-pg.", "denied", gp(7, 5, 0, 38)),
        bars(16, "Override grants", "Override events granted, per week.", "grants",
             gp(7, 5, 5, 38)),
        bars(17, "Override refusals", "Override requests refused, per week.", "refusals",
             gp(7, 5, 10, 38)),
        bars(18, "Overrides expired", "Override grants, by the week they run out (expires_at).",
             "expired",
             gp(7, 5, 15, 38)),
        bars(19, "Security incidents", "Incidents opened per week of kind main_checkout_write,"
             " two_dispatchers or lost_card_state.", "security_inc", gp(7, 4, 20, 38)),
    ]
    d = frame(UID, TITLE, p, [])
    d.update(time={"from": "now-8w", "to": "now"}, refresh="5m")
    return d


def write(out_dir=None):
    out = Path(out_dir) if out_dir else OUT
    out.mkdir(parents=True, exist_ok=True)
    (out / FILE).write_text(json.dumps(build(), indent=2, ensure_ascii=True) + "\n")
    return out / FILE


if __name__ == "__main__":
    print("wrote", write())
