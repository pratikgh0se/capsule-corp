"""The patrol's document check (docs/architecture/components/patrol.md): the agent decides, the
script checks and acts. Code, not prompt words, is the barrier. `check` is pure."""
from capsule import patrol_facts

VERBS = ("reset", "unblock", "epic", "park", "set-aside", "rerun", "continue", "note",
         "tick-restart", "kickstart", "none")
HUMAN_VERBS = ("reset", "unblock", "epic", "park", "set-aside", "rerun", "continue")
REVIEW_ROLES = ("reviewer", "validator", "guide", "security")
FAULTS = ("card", "capsule", "environment", "unknown")
JOBS = ("heal", "disk-relief", "backup")
TICK_PER_HOUR, TICK_PER_DAY, DAY = 2, 1, 86400
RELEASE_PER_DAY, RELEASE_SINCE_HUMAN, CONTINUES_PER_DAY = 1, 2, 2


def norm(doc, work):
    """`(subjects, escalate, problems)`. A subject is `{item, verb, args, fault, why, proof}`. A
    problem makes the whole document invalid. A flat `{"verb": ...}` document is one subject for
    the first work item."""
    if not isinstance(doc, dict):
        return [], False, ["document: not an object"]
    raw = doc.get("subjects")
    if raw is None and "verb" in doc and work:
        raw = [{**doc, "key": work[0]["key"]}]
    if not isinstance(raw, list) or len(raw) > patrol_facts.MAX_SUBJECTS:
        return [], False, ["document: subjects must be a list of at most 3"]
    by_key = {k: i for i in work for k in (i["key"], i["subject"])}
    subjects, problems = [], []
    for s in raw:
        item = by_key.get(s.get("key")) if isinstance(s, dict) else None
        act = (s.get("action") or {"verb": s.get("verb"), "args": s.get("args")}
               ) if isinstance(s, dict) else {}
        verb, args = act.get("verb") if isinstance(act, dict) else None, act.get("args") or {}
        if item is None:
            problems.append(f"document: subject {s.get('key') if isinstance(s, dict) else s!r}"
                            " is not in the packet")
        elif verb not in VERBS or not isinstance(args, dict):
            problems.append(f"document: verb {verb!r} is not permitted")
        elif s.get("fault", "unknown") not in FAULTS:
            problems.append(f"document: fault {s.get('fault')!r} is unknown")
        else:
            proof = args.get("known_wait") or s.get("known_wait") or s.get("pause") or s.get("cap")
            if verb == "none" and item["class"] == "why" and not proof:
                problems.append("document: `none` for a why question needs a named pause or cap")
            subjects.append({"item": item, "verb": verb, "args": args, "why": s.get("why", ""),
                             "fault": s.get("fault", "unknown"), "proof": proof})
    return subjects, doc.get("escalate") is True, problems


def _lift(act, card, facts, state, now):
    verb = act["verb"]
    if card.get("never"):
        return f"{verb}: {card['never']}"
    if card["owner"] or card["korin"]:
        return f"{verb}: an owner hold or a Korin fix card is not the patrol's to lift"
    if card["status"] not in (("triage", "blocked") if verb == "reset" else ("blocked",)):
        return f"{verb}: the card is {card['status']}"
    if (card["releases_24h"] >= RELEASE_PER_DAY
            or card["releases_since_human"] >= RELEASE_SINCE_HUMAN):
        return f"{verb}: Capsule already released this card in the last 24 h"
    return None


def _epic(act, card, facts, state, now):
    if card["status"] != "ready" or card["runs"] or not act.get("text"):
        return "epic: needs a ready card with no runs and a lane"
    return None


def _park(act, card, facts, state, now):
    if not (card["korin"] and act.get("text", "").startswith("duplicate of")):
        return "park: only a Korin fix card, with text `duplicate of <card>: <why>`"
    return None


def _continue(act, card, facts, state, now):
    if card.get("never"):
        return f"continue: {card['never']}"
    if card["continues_24h"] >= CONTINUES_PER_DAY:
        return "continue: 2 continues in 24 h already"
    return None


def _rerun(act, card, facts, state, now):
    """A review role re-run on a `review_infra` hold: the lift checks (owner, Korin, status, the
    release caps), then the role and 1 per card per 24 h."""
    why = _lift(act, card, facts, state, now)
    if why:
        return why
    last = (state.get("reruns") or {}).get(act["card"], 0)
    if card["block_kind"] != "review_infra" or act.get("role") not in REVIEW_ROLES:
        return "rerun: only a review role, on a review_infra hold"
    if now - last < DAY:
        return "rerun: 1 per card per 24 h"
    return None


def _tick(act, card, facts, state, now):
    tick = facts.get("tick") or {}
    if tick.get("marker") == "valid" or tick.get("verdict") in ("stopped", "crashloop", "unknown"):
        return ("tick-restart: a deliberate stop, a crash loop or an unread state "
                "is not the patrol's to undo")
    last = (state.get("tick_restarts") or {}).get(facts.get("board"), [])
    if tick.get("restarts_1h", 0) >= TICK_PER_HOUR or [t for t in last if now - t < DAY]:
        return "tick-restart: the tick-watch hour cap or the patrol day cap is spent"
    return None


def _kick(act, card, facts, state, now):
    return None if act.get("job") in JOBS else f"kickstart: only {', '.join(JOBS)}"


def _note(act, card, facts, state, now):
    return None if act.get("incident") and act.get("text") else "note: needs an incident and a text"


CHECKS = {"reset": _lift, "unblock": _lift, "epic": _epic, "park": _park, "continue": _continue,
          "rerun": _rerun, "tick-restart": _tick, "kickstart": _kick, "note": _note}


def refusal(act, facts, state, cfg, now):
    """Why one action may not run, or None. Eligibility is read from `facts["cards"]`."""
    verb, card = act["verb"], (facts.get("cards") or {}).get(act.get("card"))
    if act.get("board") not in (None, facts.get("board")):
        return "the board is not in the packet"
    if verb in HUMAN_VERBS and card is None:
        return f"{verb}: no open card {act.get('card')!r} on this board"
    if verb in HUMAN_VERBS and verb in ((facts.get("patrolled") or {}).get(act["card"]) or ()):
        return f"{verb}: already sent for this card in this stall"
    check = CHECKS.get(verb)
    return check(act, card, facts, state or {}, now) if check else None


def _action(sub, facts):
    item, args, verb = sub["item"], sub["args"], sub["verb"]
    card = args.get("card") or (item["subject"] if item["class"] not in ("why", "tick") else None)
    text = str(args.get("text") or args.get("branch") or sub["why"] or "patrol")[:300]
    inc = args.get("incident") or (facts.get("incident") or {}).get(item["subject"])
    return {"verb": verb, "card": card, "text": text, "key": item["key"], "job": args.get("job"),
            "role": args.get("role"), "incident": inc, "board": args.get("board")}


def _outside(sub, act, facts):
    """Why the card of a verb is not the packet's: it must be the item's own subject or, for a
    `why` question, a card of that lane; a `tick` item has no card."""
    item, card = sub["item"], act["card"]
    if card is None or sub["verb"] not in HUMAN_VERBS:
        return None
    own = {i["subject"] for i in (facts.get("work") or []) if i["class"] not in ("why", "tick")}
    lane = ((facts.get("cards") or {}).get(card) or {}).get("branch")
    if card in own or (item["class"] == "why" and lane == item["subject"]):
        return None
    return f"{sub['verb']}: card {card!r} is not a subject of the packet"


def check(doc, facts, state, cfg, now):
    """`(actions, refusals)`. An invalid document (a verb outside the list, a subject outside the
    packet, a bare `none` for a why question) gives no action and a refusal that starts with
    `document:`. Otherwise each action is checked alone: a refused one is dropped, reason kept."""
    work = facts.get("work")
    if work is None:
        work = patrol_facts.work_list(facts, state, cfg, now)
    subjects, _, problems = norm(doc, work)
    if problems:
        return [], problems
    actions, refusals = [], []
    for sub in subjects:
        if sub["verb"] == "none":
            continue
        act = _action(sub, facts)
        why = _outside(sub, act, facts) or refusal(act, facts, state or {}, cfg, now)
        if why:
            refusals.append(f"{sub['item']['key']}: {why}")
        else:
            actions.append(act)
    return actions, refusals
