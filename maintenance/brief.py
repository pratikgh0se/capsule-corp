#!/usr/bin/env python3
"""brief.py - the two scheduled messages, composed from state files."""
import time
import datetime as _dt
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # `capsule`, as board_fence.py does

import events                      # noqa: E402  (stdlib-only reader)
from capsule import host_registry  # noqa: E402
import vault_conf                  # noqa: E402

MAINT = Path(__file__).resolve().parent
STATE = events.state_root()
VAULT = vault_conf.vault()   # None when no notes vault is configured (D254)
VAULT_SUBDIR = "Knowledge"
NEEDS_REPAIR_PREFIX = "NEEDS REPAIR: "
DECISION_PREFIX = "DECISION: "
WIDTH = 48

# Tonight's schedule, mirroring the installed plists.
CADENCE_TONIGHT = [("nightly", "23:00"), ("sprint-plan", "03:00"), ("backup", "03:35"),
                   ("inbox", "06:00"), ("morning", "06:45")]

# The notify-seen keys that mean "this is waiting on the owner", read from the real key
# shapes board_bridge writes: `<tid>:pr` (:948), `<tid>:blocked:<hash>` (:959),
# `<tid>:gate:<hash>` (:929), `<tid>:review-cap` (:911) and `spec:<slug>:notified` /
# `spec:<slug>:decision-wait` (:1090, :1262).
WAITING_KEYS = {"pr": "PR", "blocked": "Stuck", "gate": "Gate", "review-cap": "Review"}
DECISION_OPEN_EVENTS = ("decision_open",)


def _now(now=None):
    return _dt.datetime.now().astimezone() if now is None else now


def header(now=None, at="08:30"):
    now = _now(now)
    left = f"== Capsule - {now.strftime('%a %d %b')} - {at} "
    return left + "=" * max(3, WIDTH - len(left))


def _age_h(ts, now):
    """Whole hours since `ts` (epoch seconds), or None when it is unknown - a
    fabricated 0 would read as "raised a moment ago", which is the one thing
    the waiting block must never say."""
    try:
        return max(0, int((now.timestamp() - float(ts)) // 3600))
    except (TypeError, ValueError):
        return None


def _age_from_date(value, now):
    """notify-seen stores the DATE a key was notified ("2026-09-10"), which is
    enough for an age in hours from local midnight of that day."""
    try:
        day = _dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    midnight = _dt.datetime.combine(day, _dt.time()).astimezone()
    return max(0, int((now - midnight).total_seconds() // 3600))


# --- WAITING ON YOU (M195) ---------------------------------------------------

def _decision_opened(state_dir, spool=None, now=None):
    """{task_id: ts} for the newest `decision_open` event per card."""
    out = {}
    for r in _read_window(state_dir or STATE, spool, _now(now).timestamp() - 30 * 86400):
        if (r.get("payload") or {}).get("event") in DECISION_OPEN_EVENTS and r.get("task_id"):
            out[r["task_id"]] = r.get("ts")
    return out


REPOS_JSON = MAINT / "repos.json"
WAITING_CAP = 8


def _active_boards(repos_path=None):
    """Board names maintenance/repos.json lists (row 208): the active board set."""
    if repos_path is None and not REPOS_JSON.exists():
        return set(host_registry.boards_served())   # no local file: the one served-boards rule
    try:
        data = json.loads(Path(repos_path or REPOS_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(data, list):
        return set()
    return {str(r["board"]) for r in data if isinstance(r, dict) and r.get("board")}


def _default_card_status(board_name, task_id):
    """The live status came from the legacy board CLI, which is gone: always unknown."""
    return "unknown"


def _resolve_card_status(task_id, card_status, active_boards):
    """Cross-check one notify-seen task id against board truth (row 208)."""
    if not active_boards:
        return "unknown"
    import board
    for name in sorted(active_boards):
        try:
            return card_status(name, task_id)
        except board.CardNotFound:
            continue
        except Exception:
            return "unknown"
    return "archived"


def _brainstorm_meta(home, bid):
    for path in sorted(Path(home).glob(f"boards/*/brainstorms/{bid}/meta.json")):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
            return meta if isinstance(meta, dict) else {}
        except (OSError, ValueError):
            return {}
    return {}


def _queued_brainstorms(rows, now, home):
    """The Slack `Not now, queue it` clicks: rows in the defer queue (budgets.defer, via
    messages.defer_actionable) that name a brainstorm. A closed brainstorm is no longer waiting."""
    seen = {}
    for r in rows:
        if not isinstance(r, dict) or r.get("kind") != "waiting":
            continue
        bid, text = str(r.get("task_id") or ""), str(r.get("text") or "")
        if not (re.fullmatch(r"[0-9a-f]{16}", bid) and text.startswith("Brainstorm - ")):
            continue
        meta = _brainstorm_meta(home, bid)
        if meta.get("status") == "closed":
            continue
        age = _age_h(r.get("ts"), now)
        if bid in seen and (age or 0) <= (seen[bid]["age_h"] or 0):
            continue                       # a repeat row for one brainstorm: keep the oldest
        seen[bid] = {"kind": "Brainstorm", "what": str(meta.get("title") or text[13:] or bid)[:60],
                     "title": "queued, not now", "age_h": age, "key": f"bs:{bid}"}
    return list(seen.values())


def _queued_from_defer(batch_path, home, now):
    try:
        import budgets
        from capsule import paths
        rows, root = budgets._read_json(budgets._batch_path(batch_path), []), paths.home(home)
    except Exception:       # an unreadable queue only hides the queued brainstorms
        return []
    return _queued_brainstorms(rows, now, root)


def waiting_items(blocked=None, seen_path=None, now=None, state_dir=None, spool=None,
                   card_status=None, active_boards=None, vault=None, home=None,
                   batch_path=None):
    """Everything waiting on the owner, oldest first."""
    now = _now(now)
    if blocked is None:
        import decisions
        blocked = decisions.blocked_decisions
    if card_status is None:
        card_status = _default_card_status
    if active_boards is None:
        active_boards = _active_boards()
    vault_path = Path(vault) if vault else VAULT
    rows = blocked() if callable(blocked) else list(blocked)
    try:
        opened = _decision_opened(state_dir, spool, now)
    except Exception:
        opened = {}
    out = []
    for item in rows:
        reason = str(item.get("reason") or "")
        if not reason.startswith(DECISION_PREFIX):
            continue
        ts = item.get("opened_ts", opened.get(item.get("task_id")))
        out.append({"kind": "Decision",
                    "what": f"{item.get('board')}/{item.get('task_id')}",
                    "title": str(item.get("title") or item.get("task_id") or "?")[:60],
                    "age_h": _age_h(ts, now)})
    try:
        # default THROUGH state_dir, not
        # straight to the module-level STATE - --state-dir is the only isolation lever
        # Tasks 5/6's shell suites have, and it must mean "read ALL state from here",
        # not "all state except this file".
        seen = json.loads(Path(seen_path or (Path(state_dir or STATE) / "notify-seen.json"))
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = {}
    out += _queued_from_defer(batch_path, home, now)
    if isinstance(seen, dict):
        for key in sorted(seen):
            parts = str(key).split(":")
            age = _age_from_date(seen.get(key), now)
            if parts[0] == "spec" and len(parts) >= 3:
                slug = parts[1]
                if vault_path is None or not (vault_path / "Specs" / f"{slug}.md").exists():
                    continue  # no such spec note - the ledger key is junk
                out.append({"kind": "Spec", "what": slug, "title": parts[2], "age_h": age})
                continue
            if len(parts) < 2:
                continue
            label = WAITING_KEYS.get(parts[1])
            if not label:
                continue
            task_id = parts[0]
            status = _resolve_card_status(task_id, card_status, active_boards)
            if status in ("done", "archived"):
                continue  # board truth says this is over - the ledger is stale
            out.append({"kind": label, "what": task_id, "title": parts[1], "age_h": age})
    # hotfix 13 (2026-09-12): notify-seen holds one key per message hash, so a card that
    # paged many times appeared once per key (36 "Stuck" lines for one card on the first
    # live v2 brief).
    uniq = {}
    for r in out:
        k = r.get("key") or r["what"]
        cur = uniq.get(k)
        if cur is None or (r["age_h"] or 0) > (cur["age_h"] or 0):
            uniq[k] = r
    out = list(uniq.values())
    out.sort(key=lambda r: (-1 if r["age_h"] is None else -r["age_h"]))
    return out


def _waiting_line(it):
    age = f"open {it['age_h']} h" if it["age_h"] is not None else "age unknown"
    return f"  * {it['kind']} - {it['what']} - {it['title']} ({age})"


def waiting_block(blocked=None, seen_path=None, now=None, state_dir=None, spool=None,
                   card_status=None, active_boards=None, vault=None, home=None,
                   batch_path=None):
    """The first block, and the only one that always prints: WAITING ON YOU - 0 is the
    best line in the brief (study section 5.2)."""
    try:
        items = waiting_items(blocked=blocked, seen_path=seen_path, now=now,
                              state_dir=state_dir, spool=spool, card_status=card_status,
                              active_boards=active_boards, vault=vault, home=home,
                              batch_path=batch_path)
    except Exception:
        return ["WAITING ON YOU - ?  (board unreadable)"]
    lines = [_waiting_line(it) for it in items]
    if len(lines) > WAITING_CAP:
        lines = lines[:WAITING_CAP] + [f"  ... and {len(lines) - WAITING_CAP} more"]
    return [f"WAITING ON YOU - {len(items)}"] + lines


# --- OVERNIGHT (M187/M197: replaces the hand-written morning report) ---------

def _jobs_summary(state_dir, now):
    ok = fail = skip = 0
    failed = []
    for f in sorted((Path(state_dir) / "jobs").glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            end = _dt.datetime.fromisoformat(d["end"])
        except Exception:
            continue
        if (now - end).total_seconds() > 86400:
            continue
        if d.get("skipped"):
            skip += 1
        elif int(d.get("exit", 1) or 0) == 0:
            ok += 1
        else:
            fail += 1
            failed.append(str(d.get("job") or f.stem))
    return ok, skip, fail, failed


def _read_window(state_dir, spool, floor_ts):
    """Rows with ts >= floor_ts."""
    rows = events.read(spool or (Path(state_dir) / "events.jsonl"),
                       since_s=max(60, int(time.time() - floor_ts)))
    return [r for r in rows if float(r.get("ts") or 0) >= floor_ts]


def overnight_block(state_dir=None, now=None, spool=None):
    """Three lines and the dead-man switch: cards, jobs, heal. Sources: the
    events spool, state/jobs/*.json, state/heal.json - nothing else."""
    now = _now(now)
    state_dir = Path(state_dir or STATE)
    try:
        rows = _read_window(state_dir, spool, now.timestamp() - 86400)
        s = events.summary(rows)
        ok, skip, fail, failed = _jobs_summary(state_dir, now)
        done = sum(1 for r in rows
                   if (r.get("payload") or {}).get("event") in ("done", "merged"))
        parked = sum(1 for r in rows
                     if (r.get("payload") or {}).get("event") == "parked")
        lines = ["OVERNIGHT",
                 f"  {done} card(s) done - {len(s['merged'])} merged - {parked} needs repair",
                 f"  jobs {ok}/{ok + fail + skip} ok, {fail} failed"
                 + (f" ({', '.join(sorted(set(failed))[:3])})" if failed else "")
                 + (f", {skip} skipped" if skip else "")]
        try:
            import heal_table
            healthy = heal_table.all_ok(str(state_dir / "heal.json"))
        except Exception:
            healthy = False
        lines.append("  heal ok" if healthy else "  heal: see the Heal section")
        return lines
    except Exception:
        return ["OVERNIGHT", "  overnight: unavailable"]


# --- BUDGET, with a consequence rather than a percentage ---------------------

def _newest_usage(state_dir, spool=None, now=None):
    now = _now(now)
    rows = _read_window(state_dir, spool, now.timestamp() - 86400)
    usage = [r for r in rows if r.get("kind") == "usage"]
    return (usage[-1].get("payload") or {}) if usage else {}


def budget_block(state_dir=None, now=None, spool=None):
    """usage-guard.json holds no percentages (spec section 4.3): the numbers come
    from the newest kind="usage" event row, the pause from the guard file."""
    now = _now(now)
    state_dir = Path(state_dir or STATE)
    try:
        p = _newest_usage(state_dir, spool, now)
        if not p:
            return []
        session = p.get("session_pct")
        week = p.get("week_pct")
        runs, ceiling = p.get("runs_today"), p.get("ceiling")
        bits = []
        if session is not None:
            bits.append(f"5-h window {int(float(session))}%")
        if week is not None:
            bits.append(f"week {int(float(week))}%")
        if runs is not None and ceiling:
            bits.append(f"runs today {int(runs)}/{int(ceiling)}")
        if not bits:
            return []
        try:
            guard = json.loads((state_dir / "usage-guard.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            guard = {}
        until = guard.get("paused_until") if isinstance(guard, dict) else None
        if until and float(until) > now.timestamp():
            # Row 208: a bare %H:%M crossing midnight reads as already past - e.g.
            # "agents paused until 04:01" printed at 08:30 for a pause that had not yet
            # expired.
            when = _dt.datetime.fromtimestamp(float(until)).astimezone().strftime("%a %H:%M")
            consequence = f"agents paused until {when}"
        elif session is not None and float(session) >= 70:
            consequence = "hold the heavy work until the window resets"
        else:
            consequence = "heavy work fine"
        return ["BUDGET", "  " + " - ".join(bits), f"  -> {consequence}"]
    except Exception:
        return []


# --- PARKED ------------------------------------------------------------------

def parked_block(cards=None, kanban=None, boards_dir=None):
    """Cards blocked with NEEDS REPAIR:. `cards` is injected by the tests; in
    production it comes from the same board reader the waiting block uses."""
    try:
        if cards is None:
            import decisions
            import board as board_mod
            cards = []
            for name in board_mod.board_names(boards_dir):
                b = decisions._board(name, kanban, boards_dir)
                for ref in b.list(statuses={"blocked"}):
                    card = b.show(ref.id)
                    cards.append({"board": name, "task_id": ref.id,
                                  "reason": card.current_block_reason})
        rows = [c for c in cards
                if str(c.get("reason") or "").startswith(NEEDS_REPAIR_PREFIX)]
        if not rows:
            return []
        lines = [f"PARKED - {len(rows)}"]
        for c in rows[:3]:
            why = str(c.get("reason") or "")[len(NEEDS_REPAIR_PREFIX):][:60]
            lines.append(f"  * {c.get('board')}/{c.get('task_id')} - {why}")
        return lines
    except Exception:
        return []


# --- the 08:30 brief ---------------------------------------------------------

CLOSING = "Nothing else needs you today."


def _board_pointer():
    """The board's file name lives in board_view.BOARD_REL - the one place allowed to
    name it (spec 7, test_contracts.NoMarkdownBoardSurgery)."""
    try:
        import board_view
        return f"Board: {board_view.BOARD_REL}"
    except Exception:
        return "Board: (see Boards)"


def brief_text(*, state_dir=None, blocked=None, cards=None, seen_path=None,
               deferred=None, budget=None, extra=None, now=None, spool=None):
    """The 08:30 brief. `extra` is report.sh's own already-composed lines (Power
    Level, retrieval, notices, retention, index) passed through
    verbatim, so those readers keep their single home."""
    now = _now(now)
    parts = [header(now, "08:30"), ""]
    parts += waiting_block(blocked=blocked, seen_path=seen_path, now=now,
                           state_dir=state_dir, spool=spool) + [""]
    parts += overnight_block(state_dir=state_dir, now=now, spool=spool) + [""]
    bud = budget_block(state_dir=state_dir, now=now, spool=spool)
    if bud:
        parts += bud + [""]
    parked = parked_block(cards=cards)
    if parked:
        parts += parked + [""]
    # Filter to the actually-non-blank lines FIRST, then decide the trailing blank on
    # that filtered list - not on the raw `extra` list.
    extra_lines = [str(line) for line in list(extra or []) if str(line).strip()]
    parts += extra_lines
    if extra_lines:
        parts.append("")
    for line in list(deferred or []):
        parts.append(str(line))
    if budget:
        parts.append(str(budget))
    if deferred or budget:
        parts.append("")
    parts += [CLOSING, _board_pointer()]
    return "\n".join(parts)


# --- the vault copy (M197: the hand-written morning report's replacement) ----

def vault_copy_path(now=None, vault=None):
    now = _now(now)
    root = Path(vault or VAULT)
    return root / VAULT_SUBDIR / f"Morning report {now.date().isoformat()}.md"


def vault_note(text, now=None):
    """The brief, as the vault note the hand-written morning report used to be.
    AI-first: frontmatter, a `## For future agent` preamble naming the job and
    the sources, then the exact message body."""
    now = _now(now)
    day = now.date().isoformat()
    # Zero matching templates shows a bare `obsidian-report`; several pick the first alphabetically.
    tpl = sorted((MAINT.parent / "launchd" / "templates").glob("*.obsidian-report.plist"))
    label = tpl[0].stem if tpl else "obsidian-report"
    return (
        "---\n"
        "type: brief\n"
        f"date: {day}\n"
        "status: final\n"
        "source: maintenance/brief.py\n"
        f"job: report (08:30, {label})\n"
        "tags: [capsule, brief]\n"
        "---\n\n"
        f"# Morning report {day}\n\n"
        "## For future agent\n\n"
        "This note is the vault copy of the 08:30 brief that was sent to the owner's\n"
        "phone. It is written by `maintenance/brief.py --vault-copy` from state\n"
        "files only (the events spool, `state/jobs/*.json`, `state/heal.json`,\n"
        "`state/usage-guard.json`, `state/notify-seen.json`, the boards) - no model\n"
        "wrote any line of it, and no human hand-writes this note any more\n"
        "(decision row 136). Re-running the job the same day overwrites this file.\n"
        "See [[Decisions/Capsule Corp - System design decisions 2026-09-06]] row 136.\n\n"
        "## Brief\n\n"
        "```\n" + text.rstrip("\n") + "\n```\n")


def write_vault_copy(text, *, vault=None, now=None, runner=None):
    """Write the vault copy and push exactly that one path."""
    if vault is None and os.environ.get("AGENT_OPS_NO_SEND"):
        print("brief: refusing the default vault under test (AGENT_OPS_NO_SEND is set "
              "and no vault= was given)", file=sys.stderr)
        return None
    if vault is None and VAULT is None:
        print(f"brief: {vault_conf.NO_VAULT}", file=sys.stderr)
        return None
    try:
        path = vault_copy_path(now=now, vault=vault)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(vault_note(text, now=now), encoding="utf-8")
        root = Path(vault or VAULT)
        rel = path.relative_to(root).as_posix()
    except Exception as exc:
        print(f"brief: vault copy failed: {exc}", file=sys.stderr)
        return None
    try:
        import vault_git
        vault_git.push(root, [rel], f"brief: {_now(now).date().isoformat()}", runner=runner)
    except Exception as exc:
        print(f"brief: vault push failed: {exc}", file=sys.stderr)
    return rel


# --- the 18:00 close (M188/M189/M190) ---------------------------------------

# The quiet_hours end (RUNBOOK "Quiet hours"); a literal, the config is per board.
QUIET_LINE = "Quiet until 07:30 unless something pages."


def _since_midnight(state_dir, now, spool=None):
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return _read_window(state_dir, spool, midnight)


def today_block(state_dir=None, now=None, spool=None):
    now = _now(now)
    try:
        rows = _since_midnight(Path(state_dir or STATE), now, spool)
        ev = lambda name: sum(1 for r in rows
                              if (r.get("payload") or {}).get("event") == name)
        done = ev("merged") + ev("done")
        pages = sum(1 for r in rows
                    if r.get("kind") == "job"
                    and (r.get("payload") or {}).get("exit") not in (None, 0))
        return ["TODAY",
                f"  {done} card(s) done - {ev('merged')} merged - {ev('rolled_back')} rolled back",
                f"  {ev('decision_answered')} decision answered - {pages} page(s)"]
    except Exception:
        return ["TODAY", "  today: unavailable"]


def tonight_block():
    return ["TONIGHT", "  " + " - ".join(f"{job} {at}" for job, at in CADENCE_TONIGHT)]


def _item_text(it):
    """One waiting item without the list bullet (leading space kept), for a sentence's tail."""
    return _waiting_line(it).split("*", 1)[1]


# The tail of statusview.board_view's problem line for a board it could not open.
BOARD_UNREADABLE = "the board cannot be opened."


def _decision_items(now=None, snapshot=None):
    """The decisions waiting on the owner, oldest first: the cards on the Capsule board's own
    NEEDS YOU list (capsule.statusview, what `capsule status` prints). Sign-off files carry no
    task ref and are not decisions. Raises (so the job fails loudly) on any error and when a
    board could not be opened: statusview turns that into an empty section plus an ATTENTION
    entry, which would otherwise read as "nothing waiting"."""
    now = _now(now)
    if snapshot is None:
        from capsule import statusview
        snapshot = statusview.snapshot
    snap = snapshot()
    broken = [p["what"] for p in snap["attention"]["open"] if p["what"].endswith(BOARD_UNREADABLE)]
    if broken:
        raise RuntimeError("; ".join(broken))
    out = []
    for n in snap["needs_you"]:
        task = (n.get("ref") or {}).get("task")
        if task:
            what = f"{n['board']} #{n['number']}" if n.get("number") else f"{n['board']}/{task}"
            out.append({"kind": "Decision", "what": what, "title": n["name"],
                        "age_h": _age_h(n.get("since"), now)})
    out.sort(key=lambda r: (-1 if r["age_h"] is None else -r["age_h"]))
    return out


def tomorrow_line(blocked=None, seen_path=None, now=None, state_dir=None, spool=None):
    try:
        items = waiting_items(blocked=blocked, seen_path=seen_path, now=now,
                              state_dir=state_dir, spool=spool)
    except Exception:
        items = []
    if not items:
        return "  nothing - the night can run"
    return "  first item:" + _item_text(items[0])


def close_text(*, state_dir=None, blocked=None, cards=None, seen_path=None, now=None,
               spool=None):
    """The 18:00 close: what shipped, what is parked, tomorrow's first item, and
    the quiet-hours contract as its last line (row 136)."""
    now = _now(now)
    parts = [header(now, "18:00"), ""]
    parts += today_block(state_dir=state_dir, now=now, spool=spool)
    parked = parked_block(cards=cards)
    parts += parked if parked else ["PARKED", "  nothing parked"]
    parts += tonight_block()
    parts += ["TOMORROW", tomorrow_line(blocked=blocked, seen_path=seen_path, now=now,
                                        state_dir=state_dir, spool=spool)]
    bud = budget_block(state_dir=state_dir, now=now, spool=spool)
    if bud:
        parts += bud[:2]
    parts += ["", QUIET_LINE]
    return "\n".join(parts)


# --- CLI ---------------------------------------------------------------------

def _opt(argv, name, default=None):
    return argv[argv.index(name) + 1] if name in argv and len(argv) > argv.index(name) + 1 else default


def _extra(argv):
    """--extra-file <path>: report.sh's own lines, one per line. A file rather
    than argv, because those lines contain newlines and `#` headings."""
    p = _opt(argv, "--extra-file")
    if not p:
        return []
    try:
        return Path(p).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def _deferred_and_budget(argv):
    if "--no-batch" in argv:
        return [], None
    deferred, budget = [], None
    try:
        import budgets
        deferred = budgets.brief_lines()
    except Exception:
        deferred = []
    try:
        import messages
        budget = messages.budget_line()
    except Exception:
        budget = None
    return deferred, budget


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    state_dir = _opt(argv, "--state-dir", str(STATE))
    # default None, not str(VAULT) - a non-None default here
    # defeated write_vault_copy's Ruling 10/16 guard entirely, since that guard only
    # fires on `vault is None`.
    vault = _opt(argv, "--vault")
    blocked = [] if "--no-board" in argv else None
    cards = [] if "--no-board" in argv else None
    if "--brief" in argv:
        # M-5: computed only here, not unconditionally above - --close never uses it, so
        # it must not read brief-batch.json/message-budget.json on the 18:00 path for a
        # value close_text() throws away.
        deferred, budget = _deferred_and_budget(argv)
        print(brief_text(state_dir=state_dir, blocked=blocked, cards=cards,
                         deferred=deferred, budget=budget, extra=_extra(argv)))
        return 0
    if "--close" in argv:
        print(close_text(state_dir=state_dir, blocked=blocked, cards=cards))
        return 0
    if "--blocked-tonight" in argv:
        # M190: one line naming the oldest waiting decision, or nothing. Errors propagate, so a
        # failing composer fails the job loudly instead of reading as "nothing blocked".
        items = _decision_items()
        if items:
            more = f" (+{len(items) - 1} more)" if len(items) > 1 else ""
            print("Blocked tonight:" + _item_text(items[0]) + more)
        return 0
    if "--vault-copy" in argv:
        # the vault copy is best-effort and always last
        # (write_vault_copy's own docstring) - the brief has already sent by the time
        # this runs, so a bad --file or a refused/failed write is an honest stderr line,
        # never a non-zero exit that could fail the job.
        src = _opt(argv, "--file", "-")
        try:
            text = sys.stdin.read() if src == "-" else Path(src).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"brief: could not read {src}: {exc}", file=sys.stderr)
            return 0
        rel = write_vault_copy(text, vault=vault)
        print(rel or "")
        return 0
    print("usage: brief.py --brief | --close | --blocked-tonight | --vault-copy [--file <path>]",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
