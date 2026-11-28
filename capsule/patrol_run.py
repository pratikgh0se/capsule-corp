"""One patrol run and its command line (docs/architecture/components/patrol.md): the steps of
9.4.2 over `capsule.patrol`'s pieces. `python3 -m capsule patrol [--board B] [--dry] [--json]`
and `python3 -m capsule why [--board B] [--json]`. Exit 0 whatever the agent did."""
import contextlib
import json
import os
import shlex
import signal
import subprocess
import sys
import time

from capsule import (answer_base, ask_apply, ask_gate, config, patrol, patrol_check, paths,
                     stall_cli, stall_owner, tick_watch)
from capsule.patrol import DAY, LOCK_S



def _used(log, board, now, state=None):
    """(cheap calls, strong calls) of this board in the last 24 h: `patrol.log` plus the calls of
    a run that has not reached its log line yet (`state["calls"]`, saved after each call)."""
    rows = [(r.get("boards") or {}).get(board) or {} for r in log if now - r.get("at", 0) < DAY]
    cur = ((state or {}).get("calls") or {}).get(board) or {}
    if now - cur.get("at", 0) < DAY:
        rows.append(cur)
    return sum(r.get("cheap", 0) for r in rows), sum(r.get("strong", 0) for r in rows)


def _count(state, board, now, kind, save):
    """One more `kind` call of this board in the open run's tally; saved at once."""
    cur = state.setdefault("calls", {}).setdefault(board, {"at": int(now), "cheap": 0, "strong": 0})
    cur[kind] += 1
    save()


def _stalls(board, cfg, home, now):
    custom = os.environ.get("CAPSULE_PATROL_STALLS_CMD")
    if custom:
        try:
            r = subprocess.run([*shlex.split(custom), board], capture_output=True, text=True,
                               timeout=120)
            return r.returncode, r.stdout
        except (OSError, subprocess.SubprocessError):
            return 2, ""
    try:
        return 0, json.dumps(stall_cli._items(board, cfg, home, now))
    except Exception:
        return 2, ""


def _why_actions(facts, state, cfg, now):
    """The acts a fixed `why` reason applies at once (no agent): set-aside and continue."""
    acts, refusals = [], []
    for lane in facts.get("why") or []:
        verb, _, card = lane["action"].partition(" ")
        if verb not in ("set-aside", "continue", "tick-restart"):
            continue
        act = {"verb": verb, "card": card or None, "text": f"patrol: {lane['reason']}",
               "key": f"why:{lane['lane']}", "board": None, "job": None, "role": None,
               "incident": None}
        bad = patrol_check.refusal(act, facts, state, cfg, now)
        if bad:
            refusals.append(f"{act['key']}: {bad}")
        else:
            acts.append(act)
    return acts, refusals


def _login():
    """True, False or None (unknown). `claude auth status`, or `CAPSULE_PATROL_LOGIN_CMD`."""
    cmd = shlex.split(os.environ.get("CAPSULE_PATROL_LOGIN_CMD") or "claude auth status")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return bool(json.loads(r.stdout).get("loggedIn"))
    except (ValueError, AttributeError):
        return r.returncode == 0


_kick = stall_owner.kickstart


def probes():
    return {"stalls": _stalls, "agent": patrol._agent, "login": _login,
            "restart": tick_watch._restart, "kick": _kick}


def _login_state(state, now, probe, wanted):
    """The login answer of today; the probe runs once on the first run of a day that needs it."""
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    cur = state.get("login") or {}
    if cur.get("day") != day and wanted:
        cur = {"day": day, "ok": probe()}
        state["login"] = cur
    return cur.get("ok")


def _skip(facts, pc, login, used):
    if facts["paused"]:
        return "pause"
    if login is False:
        return "login"
    if pc["agent_cmd"] is None:
        return "off"
    return "cap" if used >= pc["calls_per_day"] else None


def _strong(esc, subs, state, pc, used):
    """The items the strong model looks at: each `none` with an unknown fault, or all on
    `escalate`; none when the strong cap or the 1-per-stall rule stops it."""
    hard = [s["item"] for s in subs if s["verb"] == "none" and s["fault"] == "unknown"
            and s["item"]["class"] != "why"]
    items = [s["item"] for s in subs] if esc else hard
    seen = state.get("strong") or {}
    return [i for i in items if i["key"] not in seen] if used < pc["strong_per_day"] else []


def _undecided(board, home, facts, items, subs2, state, now, probes_):
    """Both models gave `none`: set a card aside with the evidence line; restart the tick once
    for a board-level stall. Returns the actions."""
    acts, said = [], {s["item"]["key"] for s in subs2 if s["verb"] != "none"}
    for item in items:
        if item["key"] in said:
            continue
        card = item["subject"] in (facts.get("cards") or {})
        acts.append({"verb": "set-aside" if card else "tick-restart",
                     "card": item["subject"] if card else None, "key": item["key"],
                     "text": "patrol could not decide", "board": None, "job": None, "role": None,
                     "incident": facts["incident"].get(item["subject"]), "item": item,
                     "note": "patrol could not decide: both models answered none"})
    return acts


def _board(home, name, cfg, state, log, now, dry, probes_, login):
    """One board's pass. Returns `(summary, applied, refusals, agent)`."""
    pc = config.patrol(cfg)
    if pc["agent_cmd"] is not None and os.environ.get("CAPSULE_PATROL_CMD"):
        pc["agent_cmd"] = os.environ["CAPSULE_PATROL_CMD"]      # tests: a fake agent command
    facts = patrol.collect(name, cfg, home, now, probes_)
    seen = {x["subject"] for x in facts["second_look"]}
    missed = {s: (state.get("missed", {}).get(name, {}).get(s, 0)) + 1 for s in seen}
    state.setdefault("missed", {})[name] = missed
    acts, refusals = _why_actions(facts, state, cfg, now)
    done = {a["card"] for a in acts}      # a fixed `why` action already covers these cards
    lane = ("no_progress", "lane_idle", "idle") if done else ()     # the lane's stall is explained
    work = [i for i in patrol.work_list(facts, {**state, "missed": missed}, cfg, now)
            if i["subject"] not in done and i["class"] not in lane]
    facts["work"] = work
    out = {"stalls": len(facts["stalls"]), "second_look": len(facts["second_look"]),
           "tick": facts["tick"].get("verdict"), "supervisor": facts["supervisor"],
           "subjects": [i["key"] for i in work], "cheap": 0, "strong": 0}
    cheap, strong = _used(log, name, now, state)
    skip = _skip(facts, pc, login, cheap)
    agent = f"skipped:{skip}" if skip else "none"

    def save():
        if not dry:
            patrol._write(home / "patrol-state.json", state)
    if not skip and work:
        agent, more, refs = _agent(home, name, pc, facts, work, (state, save), now, probes_, out,
                                   strong)
        acts, refusals = acts + more, refusals + refs
    applied = [dict(a, ok=None) for a in acts] if dry else patrol.apply(
        home, name, cfg, acts, state, now, probes_)
    save()
    out["ask_gate"] = not (dry or facts["paused"] or login is False or pc["agent_cmd"] is None)
    return out, applied, refusals, agent


def _ask_gates(home, res, probes_, now):
    """The ask gate step, last (it is slow): after every board's actions and the state are saved,
    the panel judges one dossier of an owner-held card that has no verdict, in the first board
    that has one. It logs to `<board>/asks/` only and never stops the patrol. Sets each board's
    `ask_gate` to its verdict count (-1 on an error)."""
    done = False
    for name in res["boards"]:
        if not res["dry"]:      # mode `on` only: apply due decisions, tell askers, send one ask
            with contextlib.suppress(Exception):
                ask_apply.step(home, name, now)
    for name, out in res["boards"].items():
        ready = out["ask_gate"] is True and not done
        out["ask_gate"] = 0
        if ready:
            try:
                out["ask_gate"] = len(ask_gate.sweep(home, name, now, probes_["agent"]))
            except Exception:
                out["ask_gate"] = -1
        done = done or out["ask_gate"] > 0


def _agent(home, name, pc, facts, work, saving, now, probes_, out, strong_used):
    """The cheap call and, if it asks, one strong call. Returns `(agent, actions, refusals)`. The
    call count, the looks and the strong mark are saved right after each call (`saving`)."""
    state, save = saving
    ok, doc, acts, refs = patrol._call(home, pc, facts, work, state, now, probes_, False)
    out["cheap"] = 1
    for item in work:
        state.setdefault("looks", {}).setdefault(item["key"], []).append(int(now))
    _count(state, name, now, "cheap", save)
    if not ok:
        return "failed", [], refs
    subs, esc, _ = patrol_check.norm(doc, work)
    items = _strong(esc, subs, state, pc, strong_used)
    if not items:
        return "ran", acts, refs
    notes = [{"key": s["item"]["key"], "diagnosis": s["why"]} for s in subs]
    ok2, doc2, acts2, refs2 = patrol._call(home, pc, facts, items, state, now, probes_, True,
                                           notes)
    out["strong"] = 1
    keys = {i["key"] for i in items}
    state.setdefault("strong", {}).update({k: int(now) for k in keys})
    _count(state, name, now, "strong", save)
    if not ok2:
        return "failed", [a for a in acts if a["key"] not in keys], refs + refs2
    subs2, _, _ = patrol_check.norm(doc2, items)
    extra = [a for a in _undecided(name, home, facts, items, subs2, state, now, probes_)
             if not patrol_check.refusal(a, facts, state, {}, now)]
    return "ran", [a for a in acts if a["key"] not in keys] + acts2 + extra, refs + refs2


def run(home, names, now, dry=False, probes_=None):
    """One patrol run over `names`; the result object (also the `--json` output)."""
    t0, probes_ = time.monotonic(), {**probes(), **(probes_ or {})}
    state = patrol._read(home / "patrol-state.json", {})
    log = patrol._tail(home)
    cfgs = {n: stall_cli._board_cfg(n, home)[1] for n in names}
    login = _login_state(state, now, probes_["login"],
                         any(config.patrol(c)["agent_cmd"] is not None for c in cfgs.values()))
    res = {"at": now, "boards": {}, "actions": [], "refusals": [], "dry": dry}
    agents = []
    for name in names:
        out, applied, refs, agent = _board(home, name, cfgs[name], state, log, now, dry, probes_,
                                           login)
        res["boards"][name] = out
        res["actions"] += [dict(a, board=name) for a in applied]
        res["refusals"] += refs
        agents.append(agent)
    res["agent"] = ("failed" if "failed" in agents else "ran" if "ran" in agents else next(
        (a for a in agents if a != "none"), "none"))
    fails = state.get("fails", 0)
    state["fails"] = {"failed": fails + 1, "ran": 0}.get(res["agent"], fails)
    if state["fails"] >= patrol.DOWN_AFTER:
        state.setdefault("down_since", int(now))
    else:
        state.pop("down_since", None)
    res["agent_down"], res["agent_down_since"] = "down_since" in state, state.get("down_since")
    res["ms"] = int((time.monotonic() - t0) * 1000)
    if not dry:
        _save(home, res, state)
    _ask_gates(home, res, probes_, now)
    return res


def _save(home, res, state):
    beat = {k: res[k] for k in ("at", "agent", "agent_down", "agent_down_since", "ms")}
    beat["boards"] = {n: {k: b[k] for k in ("stalls", "second_look", "tick")}
                      for n, b in res["boards"].items()}
    patrol._write(home / "patrol.beat", beat)
    line = {**beat, "boards": {n: {k: b[k] for k in ("stalls", "second_look", "tick", "subjects",
                                                      "cheap", "strong")}
                               for n, b in res["boards"].items()},
            "actions": [{k: a[k] for k in ("verb", "card", "board", "ok") if k in a}
                        for a in res["actions"]],
            "refused": res["refusals"]}
    with open(home / "patrol.log", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")
    state.pop("calls", None)      # now in the log line
    patrol._write(home / "patrol-state.json", state)


# -- the command line --------------------------------------------------------------------------

def _lock(home, now):
    """True when this run may go. A run still alive after LOCK_S is killed."""
    path = home / "patrol.lock"
    held = patrol._read(path, {})
    pid = held.get("pid")
    if isinstance(pid, int) and pid != os.getpid():
        with contextlib.suppress(OSError):
            os.kill(pid, 0)
            if now - held.get("at", 0) < LOCK_S:
                return False
            os.kill(pid, signal.SIGKILL)
    patrol._write(path, {"pid": os.getpid(), "at": int(now)})
    return True


def _names(args, home):
    return [args.board] if args.board else stall_cli._all_boards(home)


def _now():
    raw = os.environ.get("CAPSULE_PATROL_NOW")
    return (int(float(raw)) if raw else int(time.time()))


def add_parser(sub):
    p = sub.add_parser("patrol", help="the 30-minute patrol: facts, second look, at most one agent"
                                      " call per board (--dry: act on nothing)")
    p.add_argument("--board", default=None)
    p.add_argument("--dry", action="store_true", help="print the actions; write nothing")
    p.add_argument("--json", action="store_true")
    p.add_argument("--capsule-home", default=None)
    w = sub.add_parser("why", help="why is nothing running? one answer per idle lane (read-only)")
    w.add_argument("--board", default=None)
    w.add_argument("--json", action="store_true")
    w.add_argument("--capsule-home", default=None)


def handle(args, parser):
    if args.cmd == "why":
        return _why(args)
    home, now = paths.home(args.capsule_home), _now()
    names = _names(args, home)
    if not names or (args.board and not paths.board_db(args.board, home).is_file()):
        print("capsule patrol: no board file", file=sys.stderr)
        return 2
    home.mkdir(parents=True, exist_ok=True)
    if not args.dry and not _lock(home, now):
        print("capsule patrol: another run is alive", file=sys.stderr)
        return 0
    res = run(home, names, now, dry=args.dry)
    if args.json:
        print(json.dumps(res))
        return 0
    print(f"patrol: agent {res['agent']}; " + ", ".join(
        f"{n}: {b['stalls']} stalls, {b['second_look']} second look" for n, b in
        res["boards"].items()))
    for a in res["actions"]:
        print(f"  {'would ' if args.dry else ''}{a['verb']} {a['card'] or ''} ({a['board']})"
              + (" not done" if a.get("ok") is False else ""))
    for r in res["refusals"]:
        print(f"  refused: {r}")
    return 0


def _why(args):
    home, now = paths.home(args.capsule_home), _now()
    names = _names(args, home)
    if not names:
        print("capsule why: no board file", file=sys.stderr)
        return 2
    out = []
    for name in names:
        conn = answer_base.connect_ro_tolerant(paths.board_db(name, home))
        try:
            facts = {"tick": patrol._tick(name, home, now, None),
                     "work_root": str(paths.board_dir(name, home) / "work")}
            out += [dict(x, board=name) if not args.board else x
                    for x in patrol.why(conn, facts, now)]
        finally:
            conn.close()
    if args.json:
        print(json.dumps(out))
    for x in ([] if args.json else out):
        print(f"{x['lane']}: {x['reason']} -> {x['action']} ({x['ready']} ready, "
              f"{x['last_start_age_s'] // 60} min since a run started)")
    return 0
