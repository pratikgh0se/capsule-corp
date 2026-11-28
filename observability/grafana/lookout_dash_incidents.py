"""Incidents & alerts (uid incidents; W351): what Korin has open, what repeated, and what alerts
are firing, in plain words. Reads cap_incidents (the Postgres copy of the capsule incidents
table, capsule/incidents.py), v_card_story for Korin's actions, and Grafana's own alert list."""
from build_dashboards import PG, gp, pgt, stat
from build_lookout import frame, ltable
from lookout_dash_alerts import STATES, alertlist

UID, TITLE, FILE = "incidents", "Incidents & alerts", "incidents.json"
WORDS = ("case kind when 'human_intervention' then 'you stepped in' when 'contradiction' then"
         " 'rules disagreed' when 'runaway' then 'an agent ran away' when 'silent_failure' then"
         " 'something failed quietly' when 'crash' then 'an agent crashed' when 'lost_card_state'"
         " then 'a card lost its state' when 'override' then 'a check was overridden'"
         " when 'invalid_document' then 'an agent report was malformed' when 'escaped_bug' then"
         " 'a bug got through' when 'main_checkout_write' then 'main was written outside the gate'"
         " when 'repeat' then 'a problem came back' else replace(kind, '_', ' ') end")
OPEN = "status not in ('closed', 'superseded')"
SQL = {
    "open": "select count(*) as \"open incidents\" from cap_incidents where " + OPEN,
    "repeats": ("select count(*) as \"repeats\" from cap_incidents where (kind = 'repeat'"
                " or repeat_of is not null) and opened_at >= now() - interval '30 days'"),
    "opened": ("select count(*) as \"opened\" from cap_incidents"
               " where opened_at >= now() - interval '7 days'"),
    "closed": ("select count(*) as \"closed\" from cap_incidents"
               " where closed_at >= now() - interval '7 days'"),
    "by_kind": ("select " + WORDS + " as \"what happened\", count(*) as \"open\","
                " extract(epoch from now() - min(opened_at))::bigint as \"oldest\""
                " from cap_incidents where " + OPEN + " group by kind order by 2 desc"),
    "newest": ("select " + WORDS + " as \"what happened\", coalesce(task_id, '-') as card,"
               " extract(epoch from now() - opened_at)::bigint as \"opened ago\","
               " coalesce(fix_card, 'no fix card yet') as \"fix card\" from cap_incidents"
               " where " + OPEN + " order by opened_at desc limit 15"),
    "repeat_list": ("select " + WORDS + " as \"what came back\", coalesce(task_id, '-') as card,"
                    " repeat_of as \"came back from\","
                    " extract(epoch from now() - opened_at)::bigint"
                    " as \"opened ago\" from cap_incidents where (kind = 'repeat'"
                    " or repeat_of is not null) and opened_at >= now() - interval '30 days'"
                    " order by opened_at desc limit 15"),
    "korin": ("select ts as \"when\", coalesce(task_id, '-') as card, what as \"what Korin did\""
              " from v_card_story where actor = 'korin' and ts >= now() - interval '24 hours'"
              " order by ts desc limit 15"),
}
MUST_HAVE_DATA = ("open",)


def build():
    p = [
        stat(1, "Open incidents", "Problems opened and not yet closed through the gate.", PG,
             [pgt(SQL["open"])], gp(4, 6, 0, 0), unit="short",
             steps=[("green", None), ("orange", 1), ("red", 20)]),
        stat(2, "Came back, 30 days", "Repeats: the same problem again within 30 days of a close."
             " The headline number; it should stay at zero.", PG, [pgt(SQL["repeats"])],
             gp(4, 6, 6, 0), unit="short", steps=[("green", None), ("red", 1)]),
        stat(3, "Opened, 7 days", "Incidents opened in the last 7 days.", PG,
             [pgt(SQL["opened"])], gp(4, 6, 12, 0), unit="short"),
        stat(4, "Closed, 7 days", "Incidents closed in the last 7 days.", PG,
             [pgt(SQL["closed"])], gp(4, 6, 18, 0), unit="short"),
        alertlist(5, "Alerts firing now", "What Grafana says is wrong right now: firing and"
                  " pending rules. Empty means all is quiet.", gp(7, 24, 0, 4), STATES),
        ltable(6, "Open incidents by type", "Open problems grouped by what happened, with how"
               " old the oldest one is.", SQL["by_kind"], gp(8, 12, 0, 11),
               units={"oldest": "s"}),
        ltable(7, "Came back again", "Problems that returned within 30 days of being closed.",
               SQL["repeat_list"], gp(8, 12, 12, 11), units={"opened ago": "s"}),
        ltable(8, "Newest open incidents", "The 15 most recent open incidents and whether a fix"
               " card exists.", SQL["newest"], gp(9, 24, 0, 19), units={"opened ago": "s"}),
        ltable(9, "What Korin did, last 24 hours", "Korin's own steps: opened, investigated,"
               " fixed or gave up.", SQL["korin"], gp(8, 24, 0, 28)),
    ]
    d = frame(UID, TITLE, p, [])
    d["refresh"] = "30s"
    return d
