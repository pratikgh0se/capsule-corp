"""`python3 -m capsule status [--json [--refs]] [--watch]`: the screen over `statusview`
(spec section 4).
Read-only: it never writes a board and never runs the tick. Screen text never prints ids."""
import json
import sys
import time

from capsule import config, paths, statusview

LOOP_DOT = {"alive": "🟢", "stalled": "🔴", "down": "🔴"}
WATCH_MIN_S = 3.0
BLOCKED_SHOWN = 4
TITLE_MAX, HOLDERS_SHOWN = 80, 5      # a held lane names at most this many cards, trimmed


def _label(item):
    return item["name"] + (f" #{item['number']}" if item.get("number") else "")


def _ago(since, now):
    if not since:
        return ""
    mins = max(0, int(now - since) // 60)
    if mins >= 2880:
        return f" · {mins // 1440}d"
    return f" · {mins // 60}h" if mins >= 120 else f" · {mins}m"


def _running_line(r):
    quiet = f" (quiet {r['quiet_minutes']}m)" if r.get("quiet_minutes") is not None else ""
    return (f"  {r['role_emoji']} {_label(r)} · {r['role_label']} · {r['model']}"
            f" · {r['minutes']}m{quiet}")


def _outside_line(out):
    """The OUTSIDE FACTORY text: the summary plus how old each seat is (spec section 3)."""
    if not out["summary"]:
        return "nobody working outside the factory"
    ages = [f"{m}m" for m in out.get("ages_min") or []]
    if not ages:
        return out["summary"]
    return f"{out['summary']} ({'seat' if len(ages) == 1 else 'seats'} {', '.join(ages)} old)"


def _blocked_lines(blocked):
    groups = {}
    for b in blocked:
        groups.setdefault(b["reason"], []).append(_label(b))
    out = []
    for reason, names in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        more = f" +{len(names) - BLOCKED_SHOWN} more" if len(names) > BLOCKED_SHOWN else ""
        out.append(f"  🧱 {reason} ({len(names)}): {', '.join(names[:BLOCKED_SHOWN])}{more}")
    return out


def _lane_lines(lanes):
    """One line per held lane: who holds it and how many ready cards wait behind it."""
    if not lanes:
        return []
    out = ["", f"🛤️ LANES HELD ({len(lanes)})"]
    for lane in lanes:
        if lane.get("unreadable"):
            out.append(f"  🛤️ lane {lane['branch']}: cannot be read")
            continue
        how = "running" if lane["live"] else f"idle {lane['held_minutes']}m"
        names = [_label({**h, "name": h["name"][:TITLE_MAX]}) + (" (ready)" if h["ready"] else "")
                 for h in lane["holders"]]
        more = [f"+{len(names) - HOLDERS_SHOWN} more"] * (len(names) > HOLDERS_SHOWN)
        who = ", ".join(names[:HOLDERS_SHOWN] + more)
        out.append(f"  🛤️ lane {lane['branch']} held by {who} ({how})"
                   f" · {lane['waiting']} ready waiting")
    return out


def _strip_refs(value):
    if isinstance(value, dict):
        return {k: _strip_refs(v) for k, v in value.items() if k != "ref"}
    return [_strip_refs(v) for v in value] if isinstance(value, list) else value


def _to_json(snap, refs=False):
    """The JSON text of a snapshot; internal ids only with `refs` (spec section 4)."""
    return json.dumps(snap if refs else _strip_refs(snap), ensure_ascii=False)


def render(snap):
    """The screen text for one snapshot: top line, then each section in order."""
    now, att = snap["generated_at"], snap["attention"]
    boards = "  ".join(f"{LOOP_DOT[b['loop']]} {b['board']}"
                       + ("" if b["loop"] == "alive" else f" ({b['loop']})")
                       + (" · auto-landing on" if b["auto_landing"] else " · auto-landing off")
                       for b in snap["boards"])
    lines = [boards,
             f"▶️ {len(snap['running'])} running   🙋 {len(snap['needs_you'])} need you   "
             f"🧱 {len(snap['blocked'])} blocked   🔴 {len(att['open'])} unowned problems", "",
             f"▶️ RUNNING ({len(snap['running'])})"]
    if "weekly_pause" in snap:
        lines.insert(1, snap["weekly_pause"])
    lines += [_running_line(r) for r in snap["running"]] or ["  nothing running"]
    lines += ["", f"🙋 NEEDS YOU ({len(snap['needs_you'])})"]
    lines += [f"  🙋 {_label(n)} · {n['ask']}{_ago(n.get('since'), now)}"
              for n in snap["needs_you"]] or ["  nothing needs you"]
    lines += ["", f"🧱 BLOCKED ({len(snap['blocked'])})"]
    lines += _blocked_lines(snap["blocked"]) or ["  nothing blocked"]
    lines += _lane_lines(snap.get("lanes"))
    lines += ["", f"🔴 ATTENTION ({len(att['open'])})"]
    lines += [f"  🔴 {a['what']}" for a in att["open"]] or ["  nothing unowned"]
    if att["handled_count"]:
        lines.append(f"  🔧 {att['handled_count']} problems being handled")
    out = snap["outside_factory"]
    lines += ["", "🤖 OUTSIDE FACTORY",
              f"  🤖 {_outside_line(out)}"]
    lines += _switch_lines(snap["redesign"]) if "redesign" in snap else []
    return "\n".join(lines)


def _switches(home, board):
    """One board's redesign switches from its own config as it is on disk (D17); a missing file
    reads as all off, a bad block as {"invalid": message}. Never raises."""
    try:
        cfg = json.loads(paths.resolve_config(paths.home(home), board).read_text())
    except OSError:
        cfg = {}
    except ValueError as e:
        return {"invalid": f"config.json: {e}"}
    try:
        config._validate_redesign(cfg.get("redesign", {}))
        return config.redesign(cfg)
    except (ValueError, AttributeError) as e:
        return {"invalid": str(e)}


def _switch_lines(redesign):
    out = []
    for board, sw in redesign.items():
        if "invalid" in sw:
            out += ["", f"🎛️ REDESIGN SWITCHES ({board}): invalid ({sw['invalid']})"]
            continue
        out += ["", f"🎛️ REDESIGN SWITCHES ({board})"]
        out += [f"  {k}: {('on' if v else 'off') if isinstance(v, bool) else v}"
                for k, v in sw.items()]
    return out


def watch(show, interval, out=None, sleep=time.sleep, cycles=None):
    """Redraw `show()` every `interval` seconds (at least 3: each pass re-reads every board)
    until `cycles` redraws (None = until Ctrl-C, see `run`); returns 0."""
    out, interval, n = out or sys.stdout, max(interval, WATCH_MIN_S), 0
    while cycles is None or n < cycles:
        out.write("\033[H\033[2J" + show() + "\n")
        out.flush()
        n += 1
        if cycles is None or n < cycles:
            sleep(interval)
    return 0


def run(args, parser, out=None):
    out = out or sys.stdout
    if args.watch and args.json:
        parser.error("--watch redraws the screen; use --json alone for machine output")
    if args.refs and not args.json:
        parser.error("--refs only applies to --json")

    def show():
        snap = statusview.snapshot(args.capsule_home)
        snap["redesign"] = {b["board"]: _switches(args.capsule_home, b["board"])
                            for b in snap["boards"]}
        return _to_json(snap, refs=args.refs) if args.json else render(snap)

    if args.watch:
        try:
            return watch(show, args.interval, out)
        except KeyboardInterrupt:
            return 0
    out.write(show() + "\n")
    return 0
