"""The tick's side of the `unowned-work-idle` alert (moved out of `Tick`; `stall.step` calls it)."""
import sys

from capsule import db, dispatch, incidents, metrics


def check(conn, cfg, now):
    """The condition `rules.yaml`'s `unowned-work-idle` rule evaluates (detector silence: the
    tick is alive but the detector cache is not fresh, so no unowned count), evaluated here
    because Grafana has no Alertmanager webhook back into capsule
    (`incidents.record_alert_firing`). That is how a second dead-man activation within 24h
    reaches Korin. `guard_state` key `unowned_idle` tracks how long the condition has been
    true (`since`) and the last pass it fired on (`fired_at`); it fires at most once per
    episode, after `metrics.COUNT_MISSING_SECONDS` (the rule's window), and never raises. The
    incident it may open carries evidence: the cache state and the last dispatched run_id."""
    try:
        _check(conn, cfg, now)
    except Exception as exc:
        print(f"capsule tick: unowned-idle alert check failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)


def _check(conn, cfg, now):
    cache = metrics.cache_state(now=now)
    state, version = db.guard_get(conn, "unowned_idle")
    state = state or {}
    since, fired_at = state.get("since"), state.get("fired_at")
    # deadman off is silent (the rule's `watch` clause); a pause is a stop
    watched = cfg.get("deadman", True) and not dispatch.any_pause(conn, now, checks=True)
    if watched and cache != "fresh":
        since = since or now
        if now - since >= metrics.COUNT_MISSING_SECONDS and (not fired_at or fired_at < since):
            row = conn.execute(
                "SELECT run_id FROM task_runs ORDER BY started_at DESC LIMIT 1").fetchone()
            run_id = row["run_id"] if row is not None else None
            incidents.record_alert_firing(
                conn, cfg, alert=metrics.ALERT_NAME, key=metrics.ALERT_KEY,
                board=cfg.get("board"), now=now,
                evidence=[f"cache:{cache}"] + ([f"run_id:{run_id}"] if run_id else []),
                run_id=run_id)
            fired_at = now
    else:
        since = None
    db.guard_set(conn, "unowned_idle", {"since": since, "fired_at": fired_at},
                 expect_version=version, now=now)
