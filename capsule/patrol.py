"""The patrol (docs/architecture/components/patrol.md): `python3 -m capsule patrol [--board B]
[--dry] [--json]`, one run each 30 min. A script collects facts, takes its own second look, acts
on what a fixed rule answers, and starts at most one agent call per board for the rest. The agent
decides, the script checks the document and acts (`patrol_check`). `capsule why` prints the
per-lane "why is nothing running" answer. Every effect goes through `probes` (tests pass fakes)."""
import contextlib
import json
import os
import shlex
import signal
import subprocess
import sys

from capsule import (answer_base, db, dispatch, human, idle_lane, incidents, paths, patrol_check,
                     row_schemas, stall, stall_page, stall_ruling, tick_watch)
from capsule.patrol_check import check  # noqa: F401
from capsule.patrol_facts import (  # noqa: F401
    DAY, cards, second_look, why, work_list)

TURNS, STRONG_TURNS = 20, 30
LOCK_S, DOWN_AFTER, KEEP_LINES = 1200, 3, 600
WHY_Q = "Why: work is ready and nothing runs. Choose one permitted verb that makes work start."
STALL_Q = "Diagnose these stalls. Choose one permitted verb for each subject, or none."


def _read(path, default):
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def _write(path, doc):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc))
    os.replace(tmp, path)


def _tail(home):
    try:
        lines = (home / "patrol.log").read_text().splitlines()[-KEEP_LINES:]
    except OSError:
        return []
    out = []
    for line in lines:
        with contextlib.suppress(ValueError):
            out.append(json.loads(line))
    return out




# -- facts ---------------------------------------------------------------------------------------



def _tick(board, home, now, probes):
    try:
        out = tick_watch._check_board(board, home, now, False)
    except Exception:
        return {"verdict": "unknown"}
    st = tick_watch._read_json(paths.board_dir(board, home) / "tick-watch.json")
    out["restarts_1h"] = len([t for t in st.get("restarts") or [] if now - t < 3600])
    out["bad_s"] = int(now - st["bad_since"]) if st.get("bad_since") else 0
    return out


def collect(board, cfg, home, now, probes):
    """The facts of one board (read-only)."""
    rc, out = probes["stalls"](board, cfg, home, now)
    try:
        listed = json.loads(out)
        broken = rc not in (0, 1) or not isinstance(listed, list)
    except ValueError:
        listed, broken = [], True
    facts = {"board": board, "stalls": [] if broken else listed,
             "supervisor": "broken" if broken else "ok", "tick": _tick(board, home, now, probes)}
    conn = answer_base.connect_ro_tolerant(paths.board_db(board, home))
    try:
        facts["work_root"] = str(paths.board_dir(board, home) / "work")
        facts["paused"] = dispatch.any_pause(conn, now)
        facts["second_look"], facts["why"] = second_look(conn, now), why(conn, facts, now)
        facts["cards"] = cards(conn, now, cfg)
        facts["patrolled"] = _patrolled(conn, home, board)
        facts["summary"] = dict(conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY 1"))
        live = idle_lane.contract_cards(conn, now)
        facts["contract_live"] = [c for c in facts["cards"] if c in live]
        facts["owner_cards"] = [c for c, v in facts["cards"].items() if v["owner"]]
        facts["ruling"] = [r[0] for r in conn.execute(
            "SELECT task_id FROM task_runs WHERE ended_at IS NULL AND json_extract(COALESCE("
            "metadata, '{}'), '$.ruling_of') IS NOT NULL")] + [      # or a pending L1 ruling
            t["id"] for t in stall_ruling.wanted(conn)]
        ready = dict(conn.execute("SELECT branch_name, COUNT(*) FROM tasks WHERE status='ready'"
                                  " GROUP BY 1"))
        facts["waiting"] = {c: ready.get(v["branch"], 0) for c, v in facts["cards"].items()}
        recs = {k.rpartition(":")[2]: r.get("incident") for k, r in stall.records(conn).items()}
        facts["incident"] = {c: recs.get(c) or _cause(conn, c) for c in facts["cards"]}
    finally:
        conn.close()
    return facts


def _patrolled(conn, home, board):
    """`{card: [verbs]}` the patrol already sent for the card's open stall (the stall record's
    `patrol` mark) or that still wait in `<board>/human/` unhandled."""
    out = {}
    for key, rec in stall.records(conn).items():
        if (rec.get("patrol") or {}).get("verb"):
            out.setdefault(key.rpartition(":")[2], []).append(rec["patrol"]["verb"])
    hdir = answer_base.human_dir(home, board)
    for path in sorted(hdir.glob("*")) if hdir.is_dir() else []:
        doc = _read(path, {}) if path.is_file() else {}
        if doc.get("tag") == "patrol" and doc.get("task_id"):
            out.setdefault(doc["task_id"], []).append(doc.get("kind"))
    return out


def _cause(conn, card):
    row = conn.execute("SELECT id FROM incidents WHERE task_id=? AND status NOT IN ('closed',"
                       "'superseded') ORDER BY opened_at DESC LIMIT 1", (card,)).fetchone()
    return row[0] if row else None


# -- the agent -----------------------------------------------------------------------------------

def _argv(template, packet, model, turns):
    parts = shlex.split(template) if isinstance(template, str) else list(template)
    subs = {"{role_file}": str(paths.REPO / "contracts" / "patrol.md"),
            "{packet_file}": str(packet), "{model}": model, "{max_turns}": str(turns)}
    joined = "\n".join(parts)
    out = []
    for part in parts:
        for key, val in subs.items():
            part = part.replace(key, val)
        out.append(part)
    tail = ([str(packet)] if "{packet_file}" not in joined else []) + (
        [model] if "{model}" not in joined else []) + (
        [subs["{role_file}"]] if "{role_file}" not in joined else [])
    return out + tail


def _agent(argv, packet, model, timeout_s, home):
    """`(rc, stdout)`; rc is None when the command was killed at the time cap or did not start."""
    env = {**os.environ, "CAPSULE_PATROL_PACKET": str(packet), "CAPSULE_PATROL_MODEL": model}
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, cwd=str(home), env=env, start_new_session=True)
    except OSError:
        return None, ""
    try:
        out = proc.communicate(timeout=timeout_s)[0]
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        return None, ""
    return proc.returncode, out


def _doc(text):
    """The last JSON object in the agent's output (a code fence or prose around it is fine)."""
    dec, found, i = json.JSONDecoder(), None, text.find("{")
    while 0 <= i < len(text):
        try:
            obj, end = dec.raw_decode(text, i)
            found, i = (obj if isinstance(obj, dict) else found), text.find("{", end)
        except ValueError:
            i = text.find("{", i + 1)
    return found


def _packet(home, facts, work, strong, notes):
    """Line 1 is the question (it names each key, class and the tick verdict); line 2 is JSON."""
    ask = WHY_Q if any(i["class"] == "why" for i in work) else STALL_Q
    names = "; ".join(f"{i['key']} (class {i['class']})" for i in work)
    doc = {"schema": "patrol.v1", "board": facts["board"], "subjects": work,
           "tick_watch": facts["tick"], "summary": facts["summary"],
           "supervisor": facts["supervisor"], "why": facts["why"],
           "permitted_verbs": list(patrol_check.VERBS),
           "reply": {"subjects": [{"key": "", "diagnosis": "", "fault": "card|capsule|"
                                   "environment|unknown", "action": {"verb": "", "args": {}},
                                   "why": ""}], "escalate": False},
           **({"cheap_notes": notes} if notes else {})}
    path = home / "patrol" / f"packet-{facts['board']}{'-strong' if strong else ''}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    verdict = facts["tick"].get("verdict")
    path.write_text(f"{ask} Subjects: {names}. Tick: {verdict}.\n{json.dumps(doc)}\n")
    return path


def _call(home, pc, facts, work, state, now, probes, strong, notes=None):
    """`(ok, document, actions, refusals)` of one agent call. A call that is killed, exits non-zero,
    prints no document or an invalid one is a failure."""
    model = pc["strong_model"] if strong else pc["model"]
    packet = _packet(home, facts, work, strong, notes)
    base = int(os.environ.get("CAPSULE_PATROL_TIMEOUT_S") or pc["timeout_s"])
    cap = base * 3 // 2 if strong else base
    argv = _argv(pc["agent_cmd"], packet, model, STRONG_TURNS if strong else TURNS)
    rc, out = probes["agent"](argv, packet, model, cap, home)
    doc = _doc(out) if rc == 0 else None
    if doc is None:
        how = "killed" if rc is None else f"exit {rc}"
        return False, None, [], [f"agent: {how} or no document"]
    actions, refusals = check(doc, {**facts, "work": work}, state, {"patrol": pc}, now)
    bad = any(r.startswith("document:") for r in refusals)
    return not bad, doc, actions, refusals


# -- effects -------------------------------------------------------------------------------------

def _human(home, board, act, now):
    d = answer_base.human_dir(home, board)
    d.mkdir(parents=True, exist_ok=True)
    doc = {"task_id": act["card"], "kind": act["verb"], "text": act["text"], "at": int(now),
           "author": "capsule", "tag": "patrol",
           **({"role": act["role"]} if act.get("role") else {})}
    row_schemas.require("intervention.v1", doc)
    human._land(d, doc)


def _evidence(home, board, cfg, act, now):
    """One evidence line on the stall's incident (`stall_page.incident` opens it when the tick has
    not): the same incident the supervisor uses, never a second one."""
    item = act.get("item") or {}
    with contextlib.closing(db.connect(paths.board_db(board, home))) as conn:
        inc = act.get("incident") or (stall_page.incident(conn, cfg, {
            "class": item.get("class", "no_progress"), "subject": item.get("subject") or board,
            "age_s": item.get("age_s", 0), "evidence": item.get("evidence") or []}, now)
            if item else None)
        if inc:
            incidents.attach(conn, inc, [act["note"]])


def _restart(home, board, now, probes):
    ok = probes["restart"](board, home)
    path = paths.board_dir(board, home) / "tick-watch.json"
    st = tick_watch._read_json(path)
    st["restarts"] = [*(st.get("restarts") or []), int(now)]
    tick_watch._save(path, st)
    return ok


def _kick(job, probes):
    plist = next(iter(sorted(tick_watch._agents().glob(f"*.capsule-corp-{job}.plist"))), None)
    return plist is not None and probes["kick"](plist.name[:-len(".plist")])


def _mark(home, board, act, now):
    """`patrol: {at, verb, n}` in the card's stall record(s), so the tick sees the action."""
    with contextlib.closing(db.connect(paths.board_db(board, home))) as conn:
        for key, rec in stall.records(conn).items():
            if key.rpartition(":")[2] == act["card"]:
                rec["patrol"] = {"at": int(now), "verb": act["verb"],
                                 "n": (rec.get("patrol") or {}).get("n", 0) + 1}
                db.guard_set(conn, key, rec, now=now)


def apply(home, board, cfg, acts, state, now, probes):
    """Run each checked action; returns one `{verb, card, ok}` per action."""
    done = []
    for act in acts:
        try:
            ok = _one(home, board, cfg, act, state, now, probes)
        except Exception as exc:       # one failure never loses the caps or stops the rest
            print(f"capsule patrol: {act['verb']}: {type(exc).__name__}: {exc}", file=sys.stderr)
            ok = False
        done.append({"verb": act["verb"], "card": act["card"], "key": act["key"],
                     "ok": bool(ok)})
    return done


def _one(home, board, cfg, act, state, now, probes):
    """One checked action. A cap is written to `state` before the effect, so a crash keeps it."""
    verb, ok = act["verb"], True
    if verb in patrol_check.HUMAN_VERBS:
        if verb == "rerun":
            state.setdefault("reruns", {})[act["card"]] = int(now)
        _human(home, board, act, now)
        _mark(home, board, act, now)
    elif verb == "note":
        act["note"] = act["text"]
    elif verb == "tick-restart":
        bdir = paths.board_dir(board, home)
        if tick_watch.stop_marker(bdir, now, tick_watch._state_dir()) == "valid":
            print("capsule patrol: tick-restart: a deliberate stop is now on, not undone",
                  file=sys.stderr)
            return False
        state.setdefault("tick_restarts", {}).setdefault(board, []).append(int(now))
        ok = _restart(home, board, now, probes)
    elif verb == "kickstart":
        ok = _kick(act["job"], probes)
    if act.get("note"):
        _evidence(home, board, cfg, act, now)
    return ok

