"""Stall supervisor, unit 3: the silent-run facts (CPU samples, the quiet window, the container
test). Split from `stall_remedies`, which imports every name here."""
from capsule import config, seats
from capsule.db import guard_get, guard_set

CPU_GROWTH_S = 1.0
SAMPLES, SAMPLE_GAP_S, KEEP_S = "stall_cpu:", 300, 4 * 3600


def _window(conn, run_id, span):
    """(state, CPU growth, window start) from the stored samples (`step` writes them): `none` while
    no two samples are `span` apart, `unreadable` when a CPU reading was None, else `ok`."""
    got = guard_get(conn, SAMPLES + run_id)[0] or []
    if not got:
        return "none", None, None
    last = got[-1]
    base = next((s for s in reversed(got) if last[0] - s[0] >= span), None)
    if base is None:
        return "none", None, None
    if base[1] is None or last[1] is None:
        return "unreadable", None, base[0]
    return "ok", last[1] - base[1], base[0]


def _container(run):
    """A strong-tier run: its commands run in the container through the exec server, so their CPU
    is not in the host process tree and a long test looks silent. Observe-only: never killed.
    The blind tester is stored as runner `host` but runs its commands in the container too.
    Killable: host runs (reviewer, validator, guide, korin, light and mac tiers)."""
    return run["runner"] == "strong" or run["profile"] == "blind-tester"


def _silence(conn, cfg, now, p, run):
    """None, or (quiet seconds, state, growth): `proven` when the stored window is as long as the
    quiet limit, shows under CPU_GROWTH_S of growth and files were quiet across all of it; `watch`
    while the window is not there yet or files moved inside it; `unreadable` for a None CPU."""
    limit = config.stall(cfg)["silent_s"] // (2 if run["profile"] in seats.REVIEW_PROFILES else 1)
    if not run["worker_pid"] or not p["alive"](run["worker_pid"]):
        return None
    quiet = p["quiet_s"](run["run_id"])
    if quiet is None or quiet < limit:
        return None
    if _container(run):
        return quiet, "watch", None
    state, grew, start = _window(conn, run["run_id"], limit)
    if state == "ok" and grew >= CPU_GROWTH_S:
        return None
    if state == "ok":
        state = "proven" if quiet >= now - start else "watch"
    return quiet, state if state != "none" else "watch", grew


def _reading(p, pid, got, now):
    """One sample `[time, CPU, per-pid map]`. With a real `cpu_map` the CPU is a running total of
    the positive per-pid gains, so a child that exits does not lower it. An injected `cpu_s` gives
    `[time, CPU]` as it is."""
    if "cpu_map" not in p:
        return [now, p["cpu_s"](pid)]
    cur = p["cpu_map"](pid)
    if cur is None:
        return [now, None, None]            # unreadable: it proves nothing
    last = next((g for g in reversed(got) if len(g) > 2 and g[1] is not None), None)
    prev = last[2] if last else {}
    base = last[1] if last else 0.0
    gain = sum(max(0.0, v - prev.get(str(k), 0.0)) for k, v in cur.items())
    return [now, base + gain, {str(k): v for k, v in cur.items()}]


def _sample(conn, p, now):
    """Once per pass (in `step`): store each live run's CPU seconds and time, when it has none or
    its last one is SAMPLE_GAP_S old. Samples of ended runs are dropped."""
    live = set()
    for run in conn.execute("SELECT run_id, worker_pid, profile, runner FROM task_runs WHERE"
                            " ended_at IS NULL AND run_id IS NOT NULL AND worker_pid IS NOT NULL"):
        live.add(SAMPLES + run["run_id"])
        got = guard_get(conn, SAMPLES + run["run_id"])[0] or []
        if ((got and now - got[-1][0] < SAMPLE_GAP_S) or _container(run)
                or not p["alive"](run["worker_pid"])):
            continue
        keep = [s for s in got if now - s[0] <= KEEP_S]
        guard_set(conn, SAMPLES + run["run_id"], [*keep, _reading(p, run["worker_pid"], got, now)],
                  now=now)
    for (key,) in conn.execute("SELECT key FROM guard_state WHERE key LIKE ?",
                               (SAMPLES + "%",)).fetchall():
        if key not in live:
            conn.execute("DELETE FROM guard_state WHERE key=?", (key,))
