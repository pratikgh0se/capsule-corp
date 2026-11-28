"""The ask gate, unit 1 (design 2026-11-23, sections 4, 5, 7, 8): no question reaches the owner
without a complete dossier. `check` fills the script-made fields (id, fingerprint, at), checks
the dossier (shape, quotes word for word in their source), and gives a verdict by the script
rules R0 to R3. After R3 it says `panel`. Unit 2 (`run`, `sweep`) calls the two-seat panel
(`capsule/ask_panel.py`) and applies R4 to R9 to the opinions. Mode `shadow` logs a verdict to a
file and changes nothing else; `on` also queues an `escalate`.
Files: `<board>/asks/dossiers/<id>.json`, `<board>/asks/queue/<id>.json`, and the verdict log
`<board>/asks/verdicts.jsonl`. Verdicts are never board events: shadow cannot change what the
board's own readers see. `capsule ask-gate check|run|panel|show`."""
import collections
import json
import re
import sys
import time
from pathlib import Path

from capsule import (answer_base, ask_apply, ask_panel, config, db, paths, stall_owner)
from capsule.ask_log import _store, log, verdict_rows
from capsule.ask_refs import dossier_id, fingerprint, panel_args, problems

DAY = 86400
WEEK = 7 * DAY
REUSE = ("escalate", "decide", "precedent")      # an earlier verdict R1 may give again
FRESH_S = DAY                                    # `cos ask` takes an escalate this young


def _gate_tasks(conn, doc):
    """The cards one fingerprint covers: the subject, a card named by the fingerprint, and the
    cards of the incidents with that fingerprint."""
    rows = conn.execute("SELECT task_id, fix_card FROM incidents WHERE fingerprint=?",
                        (doc["fingerprint"],)).fetchall()
    return {doc["subject"], doc["fingerprint"], *(x for r in rows for x in r if x)}


def _week(conn, rows, now):
    """Decisions of the last 7 days that use the weekly budget: the board's `auto_decision` events
    or, in shadow (no event), the logged `decide` verdicts, whichever is more."""
    events = conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='auto_decision'"
                          " AND COALESCE(json_extract(payload,'$.source'),'')!='precedent'"
                          " AND created_at>?", (now - WEEK,)).fetchone()[0]
    logged = sum(1 for r in rows if r.get("verdict") == "decide" and r.get("rule") != "R1"
                 and r.get("at", 0) > now - WEEK)
    return max(events, logged)


def facts(conn, cfg, doc, now, rows):
    """What R1 to R3 read: the earlier verdict, the never-list reason, the decide-alone and veto
    counts of the fingerprint."""
    dil = config.diligence(cfg)
    horizon = now - dil["answer_ttl_days"] * DAY
    prior = [r for r in rows if r.get("dossier") == doc["id"] and r.get("at", 0) > horizon
             and r.get("verdict") in REUSE and r.get("rule") != "R1"]
    card = conn.execute("SELECT * FROM tasks WHERE id=?", (doc["subject"],)).fetchone()
    tasks = _gate_tasks(conn, doc)
    events = conn.execute(
        "SELECT kind, task_id FROM task_events WHERE kind IN ('auto_decision',"
        " 'auto_decision_vetoed') AND created_at>?", (now - dil["repeat_days"] * DAY,)).fetchall()
    counts = collections.Counter(kind for kind, task in events if task in tasks)
    return {"week": _week(conn, rows, now), "budget": config.unattended(cfg)["decisions_per_week"],
            "prior": prior[-1] if prior else None,
            "never": stall_owner.never(conn, cfg, card, now) if card else "",
            "decided": counts.get("auto_decision", 0),
            "vetoed": counts.get("auto_decision_vetoed", 0)}


def _topic(doc, topics):
    text = " ".join([doc["question"], *(o["label"] for o in doc["options"])]).lower()
    return next((w for w in topics if re.search(rf"\b{re.escape(w.lower())}", text)), "")


def _never_reason(doc, dil, never):
    """Rule R2: why only the owner may decide this, else ''."""
    topic = _topic(doc, dil["owner_topics"])
    for bad, why in ((doc["class"] != "technical", f"class is {doc['class']}"),
                     (not doc["reversible"], "not reversible"),
                     (doc["blast"] in ("repo", "outside"), f"blast is {doc['blast']}"),
                     (bool(never), f"never list: {never}"),
                     (bool(topic), f"owner topic: {topic}")):
        if bad:
            return why
    return ""


def _seat_objection(opinions):
    """Rule R5: why a seat says only the owner may decide this, else ''."""
    for o in opinions:
        for bad, why in ((not o["reversible"], "not reversible"),
                         (o["class"] != "technical", f"class is {o['class']}"),
                         (o["changes_owner_decision"], "this changes an owner decision")):
            if bad:
                return f"the {o['seat']} seat says: {why}"
    return ""


def _panel_verdict(found, opinions):
    """(verdict, rule, reason) for rules R4 to R9. The rules never read a confidence value."""
    if len({o["seat"] for o in opinions}) < 2:
        return "hold", "R4", "fewer than 2 valid opinions; try again at the next patrol"
    why = _seat_objection(opinions)
    if why:
        return "escalate", "R5", why
    if len({o["pick"] for o in opinions}) > 1:
        views = "; ".join(f"{o['seat']}: {o['pick']} ({o['reason'][:120]})" for o in opinions)
        return "escalate", "R6", "the seats pick different options: " + views
    cited = [o["precedent"]["ref"] for o in opinions if o.get("precedent")]
    if cited:
        return "precedent", "R7", f"same pick {opinions[0]['pick']}; owner decision {cited[0]}"
    if found["week"] >= found["budget"]:
        return "hold", "R9", "same pick, but the decisions of the week are used"
    return "decide", "R8", f"same pick {opinions[0]['pick']}, no precedent, budget left"


def verdict(doc, found, dil, defects=(), opinions=None):
    """(verdict, rule, reason) from the dossier, `facts` and the panel's valid opinions. Pure: no
    clock, no file. First match wins. After R3, with `opinions` None the answer is `panel`;
    with a list (maybe empty) rules R4 to R9 decide."""
    if defects:
        return "refuse", "R0", "; ".join(defects)
    if found["prior"]:
        old = found["prior"]
        return old["verdict"], "R1", f"same question answered before (logged at {old['at']})"
    why = _never_reason(doc, dil, found["never"])
    if why:
        return "escalate", "R2", why
    if found["decided"] >= dil["repeat_limit"] or found["vetoed"]:
        return "escalate", "R3", (f"{found['decided']} decided alone and {found['vetoed']}"
                                  " vetoed on this fingerprint")
    if opinions is None:
        return "panel", "R3", "no script rule decides it: it needs the panel"
    return _panel_verdict(found, opinions)


def _recent_hold(home, board, dossier_id, now):
    """True when the dossier got a `hold` after a panel call in the last 24 h: its panel
    does not run again yet, so a held dossier cannot use up the day cap."""
    return any(r.get("dossier") == dossier_id and r.get("verdict") == "hold" and r.get("paneled")
               and now - r.get("at", 0) < FRESH_S for r in verdict_rows(home, board))


def _assess(path, home, board, doc, cfg, dil, now):
    """Fill the script-made fields of `doc`, check it and give the first verdict (no panel):
    `(doc, defects, found, name, rule, reason, card)`, `card` true when the subject is a card."""
    conn = db.connect(path)
    try:
        doc = {**doc, "board": board, "at": doc.get("at", now)}
        if isinstance(doc.get("subject"), str) and doc["subject"]:
            doc.setdefault("fingerprint", fingerprint(conn, doc["subject"]))
        if isinstance(doc.get("question"), str) and isinstance(doc.get("subject"), str):
            doc.setdefault("id", dossier_id(board, doc["subject"], doc["question"]))
        defects = problems(conn, home, doc)
        found = {} if defects else facts(conn, cfg, doc, now, verdict_rows(home, board))
        name, rule, reason = verdict(doc, found, dil, defects)
        subject = doc.get("subject")
        card = isinstance(subject, str) and conn.execute(
            "SELECT 1 FROM tasks WHERE id=?", (subject,)).fetchone()
    finally:
        conn.close()
    return doc, defects, found, name, rule, reason, card


def check(home, board, doc, now=None, runner=None, panel=False):
    """Run the gate on one dossier (a dict); return the result dict. Mode `off` does nothing. With
    `panel` true, a case no script rule decides goes to the panel (`runner` has the shape of
    `patrol._agent`; None means the real one) and R4 to R9 give the verdict."""
    now = int(now or time.time())
    cfg = config.load(paths.resolve_config(home, board))
    dil = ask_apply.effective(home, board, cfg, now)
    path = paths.board_db(board, home)
    if not path.is_file():
        raise ValueError(f"no board {board!r} under {home}")
    if dil["mode"] == "off":
        return {"verdict": "off", "rule": "", "reason": "mode off", "dossier": doc.get("id")}
    doc, defects, found, name, rule, reason, card = _assess(path, home, board, doc, cfg, dil, now)
    paneled, opinions = False, None
    if panel and name == "panel":
        if _recent_hold(home, board, doc["id"], now):
            return {"verdict": "hold", "rule": "R4", "dossier": doc["id"], "mode": dil["mode"],
                    "reason": "held less than 24 h ago: the panel runs once a day for a dossier"}
        args = panel_args(home, board, doc, cfg, now, runner)
        opinions = ask_panel.run_panel(*args)
        paneled = bool(args[5].get("called"))
        name, rule, reason = verdict(doc, found, dil, (), opinions)
    out = {"verdict": name, "rule": rule, "reason": reason, "dossier": doc.get("id"),
           "mode": dil["mode"], **_pick(name, rule, opinions)}
    log(home, board, {"at": now, "subject": doc["subject"] if card else "ask_gate",
                      "fingerprint": doc.get("fingerprint"), **out, "paneled": paneled})
    if not defects:
        _store(home, board, doc, name, dil["mode"])
    if dil["mode"] == "on":
        ask_apply.step(home, board, now)
    return out


def _pick(name, rule, opinions):
    """The option a `decide` or `precedent` verdict chose, and the owner decisions it cites; the
    log row carries them so a later step can apply the verdict (an R1 repeat carries none)."""
    if name not in ask_apply.SOURCES:
        return {}
    if rule == "R1":      # an earlier row holds the pick and was applied there
        return {}
    cites = [o["precedent"]["ref"] for o in opinions or [] if o.get("precedent")]
    return {"pick": opinions[0]["pick"], "cites": cites}


def load_dossier(home, board, dossier_id):
    """The dossier file of `<board>/asks/dossiers/<id>.json` as a dict."""
    if not re.fullmatch(r"dq_[0-9a-f]{8}", dossier_id):
        raise ValueError(f"not a dossier id: {dossier_id!r}")
    path = paths.board_dir(board, home) / "asks" / "dossiers" / f"{dossier_id}.json"
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        raise ValueError(f"no dossier {dossier_id} for board {board!r}") from None
    if not isinstance(doc, dict):
        raise ValueError("the dossier is not a JSON object")
    return doc


def run(home, board, dossier_id, now=None, runner=None):
    """The whole gate, panel included, on a stored dossier."""
    return check(home, board, load_dossier(home, board, dossier_id), now, runner, panel=True)


def _held(conn, home, board, dossier_id):
    try:
        subject = load_dossier(home, board, dossier_id).get("subject")
    except ValueError:
        return False
    card = conn.execute("SELECT * FROM tasks WHERE id=?", (subject,)).fetchone()
    return bool(card) and stall_owner.owner_held(conn, card)


def _panel_only(home, board, dossier_id, now=None, runner=None):
    """Run only the two seats on a stored dossier; the valid opinions. It counts against
    `diligence.calls_per_day` and writes the opinion log, but gives no verdict."""
    cfg = config.load(paths.resolve_config(home, board))
    doc = load_dossier(home, board, dossier_id)
    return ask_panel.run_panel(*panel_args(home, board, doc, cfg, int(now or time.time()), runner))


def _waiting(home, board, now):
    """The ids of stored dossiers with no final verdict yet (`hold` and `panel` are not final),
    whose card waits for the owner."""
    done = {r.get("dossier") for r in verdict_rows(home, board)
            if r.get("verdict") in (*REUSE, "refuse")}
    base = paths.board_dir(board, home) / "asks" / "dossiers"
    ids = [p.stem for p in sorted(base.glob("dq_*.json"))] if base.is_dir() else []
    conn = answer_base.connect_ro_tolerant(paths.board_db(board, home))
    try:
        return [did for did in ids if did not in done and not _recent_hold(home, board, did, now)
            and _held(conn, home, board, did)]
    finally:
        conn.close()


def sweep(home, board, now=None, runner=None):
    """The patrol step: run the gate for one owner-held card with a dossier and no verdict (one
    for each patrol run, so the step stays short). Returns the result dicts. It writes only under
    `<board>/asks/` (mode `off` does nothing), so in mode `shadow` no board state changes."""
    now = int(now or time.time())
    cfg = config.load(paths.resolve_config(home, board))
    if config.diligence(cfg)["mode"] == "off" or not paths.board_db(board, home).is_file():
        return []
    for did in _waiting(home, board, now):
        try:
            return [run(home, board, did, now, runner)]
        except Exception as exc:
            print(f"capsule ask-gate: {did} failed: {exc}", file=sys.stderr)
    return []


def show(home, board, dossier=None):
    """One text line per logged verdict of the board (oldest first), or of one dossier."""
    return [f"{r.get('at')} {r.get('subject')} {r.get('dossier')} {r.get('verdict')}"
            f" {r.get('rule')} {r.get('reason')}"
            for r in verdict_rows(home, board) if dossier in (None, r.get("dossier"))]


def add_parser(sub):
    gate = sub.add_parser("ask-gate", help="the owner-question gate: check, run, show")
    gate_sub = gate.add_subparsers(dest="gate_cmd", required=True)
    chk = gate_sub.add_parser("check", help="check one dossier (a JSON file, or - for stdin)")
    chk.add_argument("board")
    chk.add_argument("dossier_file")
    run_ = gate_sub.add_parser("run", help="run the gate with the panel on a stored dossier")
    run_.add_argument("board")
    run_.add_argument("dossier_id")
    pan = gate_sub.add_parser("panel", help="run only the two seats on a stored dossier")
    pan.add_argument("board")
    pan.add_argument("dossier_id")
    shw = gate_sub.add_parser("show", help="list the gate's verdicts for a board")
    shw.add_argument("board")
    shw.add_argument("--dossier", default=None)
    for p in (chk, run_, pan, shw):
        p.add_argument("--capsule-home", default=None)


def _run(args, home):
    if args.gate_cmd == "show":
        return "\n".join(show(home, args.board, args.dossier)), 0
    if args.gate_cmd == "panel":
        return json.dumps(_panel_only(home, args.board, args.dossier_id)), 0
    if args.gate_cmd == "run":
        out = run(home, args.board, args.dossier_id)
        return json.dumps(out), int(out["verdict"] == "refuse")
    raw = sys.stdin.read() if args.dossier_file == "-" else Path(args.dossier_file).read_text()
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise ValueError("the dossier is not a JSON object")
    out = check(home, args.board, doc)
    return json.dumps(out), int(out["verdict"] == "refuse")


def handle(args, parser):
    try:
        text, rc = _run(args, paths.home(args.capsule_home))
    except (OSError, ValueError) as exc:
        print(f"capsule ask-gate: {exc}", file=sys.stderr)
        return 1
    print(text)
    return rc
