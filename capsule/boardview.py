"""`python3 -m capsule board [<board>]`: a read-only terminal browser over one board (visibility
spec 2026-10-21, decision C, card 10). Same classifiers as `statusview`; no raw ids on screen.
Rendering is pure (rows -> lines); curses I/O is the thin shell at the bottom."""
import json
import sys
import time

from capsule import db, paths, statusview, unblocked

GROUPS = (("needs", "🙋", "needs you"), ("running", "▶️", "running"),
          ("blocked", "🧱", "blocked"), ("waiting", "⏳", "waiting"))
EMOJI = {key: emoji for key, emoji, _ in GROUPS}
LABEL = {key: label for key, _, label in GROUPS}
EVENTS_SHOWN = 5
HELP = "j/k move  Enter open  r refresh  q quit"


def age(secs):
    mins = max(0, int(secs)) // 60
    return f"{mins // 1440}d" if mins >= 2880 else f"{mins // 60}h" if mins >= 120 else f"{mins}m"


def open_board(home, board):
    """The board's db, opened read-only (writes raise)."""
    return db.connect_ro_tolerant(paths.board_db(board, home))


def load(conn, now):
    """Open tasks of one board as plain dicts, grouped in GROUPS order (statusview classifiers)."""
    decisions = statusview._decisions(conn)
    running = {r["id"]: r for r in statusview._running(conn)}
    changed = dict(conn.execute(
        "SELECT task_id, MAX(created_at) FROM task_events GROUP BY task_id").fetchall())
    out = []
    for row in conn.execute("SELECT * FROM tasks WHERE status NOT IN ('done','archived')"):
        if statusview._needs_ask(row, decisions):
            group = "needs"
        elif row["id"] in running:
            group = "running"
        elif (row["status"] in ("blocked", "triage", "parked")
              or row["block_kind"] == "dependency"):
            group = "blocked"
        else:
            group = "waiting"
        run = running.get(row["id"])
        role = statusview.ROLES.get(run["run_profile"], ("🤖", run["run_profile"] or "agent"))[1] \
            if run else None
        out.append({
            "id": row["id"], "number": statusview._number(row), "name": statusview._name(row),
            "title": statusview.plain(row["title"], 200), "group": group,
            "state": LABEL[group] if group != "waiting" else row["status"],
            "assignee": row["assignee"], "role": role, "branch": row["branch_name"],
            "since": changed.get(row["id"]) or row["created_at"] or now,
            "events": [(e["created_at"], statusview.plain(str(e["kind"]).replace("_", " ")))
                       for e in conn.execute(
                           "SELECT kind, created_at FROM task_events WHERE task_id=?"
                           " ORDER BY id DESC LIMIT ?", (row["id"], EVENTS_SHOWN))]})
    out.sort(key=lambda t: ([g[0] for g in GROUPS].index(t["group"]), t["since"]))
    return out


def _label(t):
    return t["name"] + (f" #{t['number']}" if t["number"] else "")


def render_list(tasks, now, selected=None):
    """Screen lines: one `emoji Name #n  state  age` row per task, section headers between."""
    lines, shown = [], None
    for i, t in enumerate(tasks):
        if t["group"] != shown:
            shown = t["group"]
            n = sum(1 for x in tasks if x["group"] == shown)
            lines += ([""] if lines else []) + [f"{EMOJI[shown]} {LABEL[shown].upper()} ({n})"]
        mark = ">" if i == selected else " "
        lines.append(f"{mark} {EMOJI[shown]} {_label(t)}  {t['state']}  {age(now - t['since'])}")
    return lines or ["  nothing open on this board"]


def _render_detail(t, now):
    who = t["assignee"] or "nobody"
    lines = [f"{EMOJI[t['group']]} {_label(t)}", t["title"], "",
             f"state: {t['state']} ({age(now - t['since'])})",
             f"assignee: {who}" + (f" · running as {t['role']}" if t["role"] else "")]
    if t["branch"]:
        lines.append(f"branch: {t['branch']}")
    lines += ["", "recent events:"]
    lines += [f"  {age(now - at)} ago · {what}" for at, what in t["events"]] or ["  none yet"]
    return lines + ["", "Esc/b back   q quit"]


def _window(lines, height):
    """`lines` cut to fit `height` rows with the `>` row kept visible and HELP as the last row."""
    room = max(1, height - 2)
    sel = next((i for i, ln in enumerate(lines) if ln.startswith(">")), 0)
    top = max(0, min(sel - room + 1, len(lines) - room)) if sel >= room else 0
    return lines[top:top + room] + [HELP]


def _browse(scr, home, board):
    import curses
    curses.curs_set(0)
    sel, detail, tasks, reload = 0, False, [], True
    while True:
        if reload:
            conn = open_board(home, board)
            try:
                tasks = load(conn, int(time.time()))
            finally:
                conn.close()
            sel, detail, reload = min(sel, max(0, len(tasks) - 1)), False, False
        now = int(time.time())
        height, width = scr.getmaxyx()
        lines = (_render_detail(tasks[sel], now) if detail and tasks
                 else _window(render_list(tasks, now, sel), height))
        scr.erase()
        for y, ln in enumerate(lines[:height - 1]):
            scr.addnstr(y, 0, ln, width - 1)
        key = scr.getch()
        if key == ord("q"):
            return 0
        if key == ord("r"):
            reload = True
        elif detail and key in (27, ord("b")):
            detail = False
        elif not detail and key in (10, 13, curses.KEY_ENTER) and tasks:
            detail = True
        elif not detail and key in (curses.KEY_DOWN, ord("j")):
            sel = min(sel + 1, len(tasks) - 1)
        elif not detail and key in (curses.KEY_UP, ord("k")):
            sel = max(sel - 1, 0)


def run(args, out=None):
    out = out or sys.stdout
    home = paths.home(args.capsule_home)
    board = args.board or unblocked.configured_boards(home)[0]
    if not paths.board_db(board, home).exists():
        sys.stderr.write(f"no board named {board} under {home}\n")
        return 2
    if args.once or not (out.isatty() and sys.stdin.isatty()):
        now, conn = int(time.time()), open_board(home, board)
        try:
            out.write("\n".join(render_list(load(conn, now), now)) + "\n")
        finally:
            conn.close()
        return 0
    import curses
    try:
        return curses.wrapper(_browse, home, board)
    except KeyboardInterrupt:
        return 0
