"""`python3 -m capsule plan check|cut`: a deterministic gate for a signed plan, and a cutter that
files the plan's cards through the existing intake (design 2026-09-24 §3.2-3.3; spec plan-cut).
The parse and the gate are pure (no writes); only `cut` touches the board's intake spool, and the
board database is only ever opened read-only (the tick is its only writer)."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

CARD_RE = re.compile(r"^(?:###\s+|\*\*)([A-Z]+[0-9]+[a-z]?) [-–] (.+?)(?:\*\*)?\s*$")
BADCARD_RE = re.compile(r"^(?:###\s+|\*\*)[A-Za-z]+[0-9]\w*")
HEADING_RE = re.compile(r"^#{1,2}\s")
FIELD_RE = re.compile(r"^(?:\*\*)?(Files|Check|Expect|Size|Risk|Deps|Needs|Cites|Body)"
                      r"(?::\*\*|\*\*:|:)\s*(.*?)\s*$")
HEADER_RE = re.compile(r"^(Signed|Spec|Epic|Slice|Preview|Trace|Design|Items-check):\s*(.*?)\s*$")
DATE = r"\d{4}-\d{2}-\d{2}"
SIZES = ("S", "M", "L")


def _csv(text):
    return [x.strip() for x in (text or "").split(",") if x.strip()]


def parse_plan(text):
    """(header dict, cards, problems): cards in plan order, each a dict of id, title, fields,
    body. Fenced lines (between ``` lines) are card body only; problems are malformed card
    headers and repeated field lines, as gate failure strings."""
    header, cards, problems, cur, fence = {}, [], [], None, 0   # fence: opening line, 0 = closed
    for n, line in enumerate(text.splitlines(), 1):
        marker = line.strip().startswith("```")
        fence = (0 if fence else n) if marker else fence
        m = CARD_RE.match(line)
        if marker or fence:
            _card_line(cur, line, problems, fenced=True)
        elif m:
            cur = {"id": m.group(1), "title": m.group(2), "fields": {}, "body": []}
            cards.append(cur)
        elif BADCARD_RE.match(line):
            cur = None
            problems.append(f"plan line {n}: malformed card header {line.strip()!r}")
        elif HEADING_RE.match(line):
            cur = None
        elif cur is not None:
            _card_line(cur, line, problems)
        elif not cards:
            _header_line(header, line)
    if fence:
        problems.append(f"plan: code fence opened at line {fence} is never closed")
    for c in cards:
        c["body"] = "\n".join(c["body"]).strip()
    return header, cards, problems


def _header_line(header, line):
    """A header line (HEADER_RE) before the first card; the first of each wins."""
    h = HEADER_RE.match(line.strip())
    if h and h.group(1) not in header:
        header[h.group(1)] = h.group(2)


def _card_line(card, line, problems, fenced=False):
    """One line inside a card (card None: before the first card, dropped). An unfenced line is
    a field (first wins, a repeat is a problem) or body; a fenced one is always body."""
    if card is None:
        return
    f = None if fenced else FIELD_RE.match(line.strip())
    if f and f.group(1) != "Body":
        if f.group(1) in card["fields"]:
            problems.append(f"{card['id']}: {f.group(1)}: given more than once")
        card["fields"].setdefault(f.group(1), f.group(2))
    else:
        card["body"].append(f.group(2) if f else line)


def _poc_branch(spec_dir, name):
    """True when poc/<name> is a local branch or a remote-tracking one (exact names only)."""
    git = ["git", "-C", str(spec_dir)]
    try:
        if subprocess.run(git + ["rev-parse", "--verify", "--quiet", f"refs/heads/poc/{name}"],
                          capture_output=True, timeout=20).returncode == 0:
            return True
        out = subprocess.run(git + ["for-each-ref", "--format=%(refname)", "refs/remotes"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    for ref in out.stdout.splitlines():
        remote, _, tail = ref[len("refs/remotes/"):].partition("/")
        if remote and tail == f"poc/{name}":
            return True
    return False


def _gate_spec(spec_path):
    try:
        lines = [x.strip() for x in spec_path.read_text(encoding="utf-8").splitlines()]
    except (OSError, UnicodeDecodeError) as exc:
        return [f"spec {spec_path}: unreadable ({exc})"]
    bad = []
    if not any(x.startswith("```mermaid") for x in lines):
        bad.append("spec: no mermaid diagram block")
    if not any(re.match(rf"^Diagrams approved:\s*{DATE}\b", x) for x in lines):
        bad.append("spec: no 'Diagrams approved: <YYYY-MM-DD>' line")
    pocs = {m.group(1).strip() for x in lines if (m := re.match(r"^POC (.+?):\s*\S", x))}
    for x in lines:
        m = re.match(r"^Unknown:\s*(\S.*)$", x)
        if not m:
            continue
        name = m.group(1).strip()
        if name not in pocs:
            bad.append(f"spec: unknown '{name}' has no 'POC {name}: <path>' line")
        if not _poc_branch(spec_path.parent, name):
            bad.append(f"spec: unknown '{name}' has no branch poc/{name}")
    if not any(re.match(rf"^Research validated:\s*{DATE}\b", x) or x == "Research: none needed"
               for x in lines):
        bad.append("spec: no 'Research validated: <YYYY-MM-DD>' or 'Research: none needed' line")
    return bad


def _gate_cards(cards):
    if not cards:
        return ["plan: no cards"]
    bad, seen = [], set()
    ids = {c["id"] for c in cards}
    for c in cards:
        if c["id"] in seen:
            bad.append(f"{c['id']}: duplicate card ID")
        seen.add(c["id"])
        for key in ("Files", "Check"):
            if not c["fields"].get(key):
                bad.append(f"{c['id']}: missing {key}:")
        for dep in _csv(c["fields"].get("Deps")):
            if dep not in ids:
                bad.append(f"{c['id']}: Deps names {dep}, which is not a card in this plan")
    return bad + _cycles(cards)


def _cycles(cards):
    """One line naming every card that waits, through `Deps:`, on itself."""
    deps = {c["id"]: _csv(c["fields"].get("Deps")) for c in cards}
    loop = []
    for start in deps:
        seen, todo = set(), list(deps[start])
        while todo:
            x = todo.pop()
            if x not in seen:
                seen.add(x)
                todo += deps.get(x, [])
        if start in seen:
            loop.append(start)
    return [f"plan: Deps form a cycle between {', '.join(sorted(loop))}"] if loop else []


def _out(text):
    """Print as plan_trace does, so a lone surrogate in an id never raises UnicodeEncodeError."""
    from capsule import plan_trace
    plan_trace._out(text)


def _spec_file(spec):
    return spec if spec is not None and spec.is_file() else None


def _lints(plan_path, header, cards, spec):
    """The preview lint (U2) then the trace lint (U1), both pure; their refusal lines."""
    from capsule import plan_preview, plan_trace
    return (plan_preview.problems(header, cards)
            + plan_trace.problems(plan_path, header, cards, _spec_file(spec)))


def gate(plan_path):
    """(header, cards, spec path or None, failures) for a plan file; nothing is written."""
    plan_path = Path(plan_path)
    try:
        header, cards, bad = parse_plan(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        return {}, [], None, [f"plan {plan_path}: unreadable ({exc})"]
    spec = None
    if not re.search(rf"\S.*{DATE}$", header.get("Signed", "")):
        bad.append("plan: no 'Signed: <name> <YYYY-MM-DD>' line")
    if not header.get("Spec"):
        bad.append("plan: no 'Spec: <path>' line")
    else:
        spec = Path(os.path.abspath(plan_path.parent / header["Spec"]))
        if spec.is_file():
            bad += _gate_spec(spec)
        else:
            bad.append(f"plan: Spec file {spec} does not exist")
    return header, cards, spec, bad + _gate_cards(cards) + _lints(plan_path, header, cards, spec)


def _card_fields(card):
    """The card_fields.clean input for one card; raises FieldsRefused naming the problem."""
    from capsule import card_fields
    f = card["fields"]
    size = f.get("Size") or None
    if size is not None and size not in SIZES:
        raise card_fields.FieldsRefused(f"size_tag {size!r} is not one of {'|'.join(SIZES)}")
    return card_fields.clean({"check_cmd": f.get("Check"), "check_expect": f.get("Expect") or None,
                              "size_tag": size, "risk_flags": f.get("Risk") or None})


def _filed(home, board, fingerprints):
    """The fingerprints already filed: pending intake files, or an intake_ingested event read
    from the board db opened read-only."""
    from capsule import db, intake, paths
    found = set()
    d = intake._dir(home, board)
    for p in (sorted(d.glob("rq_*.json")) if d.is_dir() else []):
        try:
            found.add(json.loads(p.read_text()).get("fingerprint"))
        except (OSError, ValueError, AttributeError):
            continue
    dbp = paths.board_db(board, home)
    if dbp.is_file():
        conn = db.connect_ro_tolerant(dbp)
        try:
            for row in conn.execute("SELECT json_extract(payload, '$.fingerprint') FROM task_events"
                                    " WHERE kind='intake_ingested'"):
                found.add(row[0])
        finally:
            conn.close()
    return found & set(fingerprints)


def _plan_key(plan):
    """Stable identity of a plan: its path inside its git repo, else its real path, so a
    worktree or a /tmp vs /private/tmp spelling never refiles the same plan."""
    real = os.path.realpath(plan)
    try:
        top = subprocess.run(["git", "-C", os.path.dirname(real), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return real
    if top.returncode != 0 or not top.stdout.strip():
        return real
    return os.path.relpath(real, os.path.realpath(top.stdout.strip()))


def _body(card, plan, spec):
    f = card["fields"]
    needs = _csv(f.get("Needs"))
    return (f"{card['body']}\n\nplan: {plan}\nspec: {spec}\ncites: {f.get('Cites') or 'none'}\n"
            f"deps: {', '.join(_csv(f.get('Deps'))) or 'none'}"
            + (f"\nneeds: {', '.join(needs)}" if needs else "")).strip()


def _parents(cards, edges):
    """{card id: parent ids}: each `Deps:` entry, then each preview edge [child, parent]."""
    out = {c["id"]: _csv(c["fields"].get("Deps")) for c in cards}
    for child, parent in edges:
        out.setdefault(child, []).append(parent)
    return {k: list(dict.fromkeys(v)) for k, v in out.items()}


def _deps_ready_off(home, board):
    """The refusal line when the board's `deps_ready` switch is off, else None."""
    from capsule import config, paths
    try:
        cfg = config.load(paths.resolve_config(home, board))
    except (OSError, ValueError, KeyError) as exc:
        return f"plan: board {board} config is unreadable ({exc}); cannot check deps_ready"
    if config.redesign(cfg)["deps_ready"]:
        return None
    return f"plan: board {board} has deps_ready off; the preview hold would not work"


def _units(prepared, extra, plan, spec):
    """(id, title, body, files, fields, kind) for each plan card, then each preview card."""
    out = [(c["id"], c["title"], _body(c, plan, spec), _csv(c["fields"].get("Files")), f, "card")
           for c, f in prepared]
    return out + [(x["id"], x["title"], f"{x['body']}\n\nplan: {plan}\nspec: {spec}", [], None,
                   x["marker"]) for x in extra]


def _cut(args):
    from capsule import card_fields, intake, paths, plan_preview
    plan = os.path.abspath(args.plan)
    header, cards, spec, bad = gate(plan)
    prepared = []
    for c in cards if not bad else []:
        try:
            prepared.append((c, _card_fields(c)))
        except card_fields.FieldsRefused as exc:
            bad.append(f"{c['id']}: {exc}")
    home = paths.home(args.capsule_home)
    extra, edges = plan_preview.derive(header, cards) if not bad else ([], [])
    linked = any(_csv(c["fields"].get("Deps")) for c in cards)
    if (extra or linked) and not bad:
        off = _deps_ready_off(home, args.board)
        if off and extra:
            bad = [off]
        elif off:
            _out(f"plan: board {args.board} has deps_ready off; Deps links will not hold cards")
    if bad:
        _out("\n".join(bad))
        return 1
    key = _plan_key(plan)
    fps = {i: f"plan:{key}:{i}" for i in [c["id"] for c in cards] + [x["id"] for x in extra]}
    done = _filed(home, args.board, fps.values())
    parents = _parents(cards, edges)
    epic = args.epic or header.get("Epic") or None
    for cid, title, body, files, fields, kind in _units(prepared, extra, plan, spec):
        if fps[cid] in done:
            _out(f"{cid} already filed")
            continue
        if args.dry_run:
            _out(f"{cid} would file: {cid} - {title}")
        else:
            _out(f"{cid} " + intake.enqueue(
                home, args.board, kind=kind, fingerprint=fps[cid], title=f"{cid} - {title}",
                body=body, source_board="operator", source_task=None, epic=epic, file_set=files,
                name=None, fields=fields, depends_on=[fps[p] for p in parents.get(cid, [])]))
        for p in parents.get(cid, []):
            _out(f"{cid} waits for {p}")
    return 0


def add_parser(sub):
    plan_p = sub.add_parser("plan", help="gate a signed plan (check) and file its cards (cut)")
    psub = plan_p.add_subparsers(dest="plan_cmd", required=True)
    psub.add_parser("check", help="run the gate only").add_argument("plan")
    cut_p = psub.add_parser("cut", help="run the gate, then file one intake card per plan card")
    cut_p.add_argument("plan")
    cut_p.add_argument("--board", required=True)
    cut_p.add_argument("--epic", default=None)
    cut_p.add_argument("--dry-run", action="store_true")
    cut_p.add_argument("--capsule-home", default=None)


def handle(args, parser):
    if not Path(args.plan).is_file():
        print(f"capsule plan: no such plan file {args.plan}", file=sys.stderr)
        return 2
    if args.plan_cmd == "cut":
        return _cut(args)
    header, cards, spec, bad = gate(args.plan)
    if bad:
        _out("\n".join(bad))
        return 1
    from capsule import plan_trace
    _, items, reason = plan_trace.analyse(args.plan, header, cards, _spec_file(spec))
    _out("\n".join([f"gate: ok ({len(cards)} cards)"] + plan_trace.summary(items, reason)))
    return 0
