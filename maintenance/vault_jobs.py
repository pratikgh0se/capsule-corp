"""vault_jobs.py - the deterministic vault jobs (no model)."""
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import board      # noqa: E402
import vault_git  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from capsule import host  # noqa: E402


def _column(name):
    """The board-file heading text for one column, derived from `board.COLUMN_NAMES`."""
    for heading in board.COLUMN_NAMES:
        body = heading[3:]
        if body.split(" ", 1)[-1] == name:
            return body
    raise KeyError(f"no board column named {name!r} in board.COLUMN_NAMES")


DONE_COL = _column("Done")
BACKLOG_COL = _column("Backlog")
SPEC_READY_COL = _column("Spec Ready")
SPRINT_COL = _column("Sprint")
IN_PROGRESS_COL = _column("In Progress")
REVIEW_COL = _column("Review")
CARD_RE = re.compile(r"^\s*- \[[ xX]\]")
UNTICKED_RE = re.compile(r"^\s*- \[ \]")   # backlog depth counts open cards only
SKIPPED = "vault not configured, skipped"
# The real checker (notes-vault skill scripts/vault_health.py) emits error / warning
# / info; "critical" is kept because older notes and the spec use it, and anything else
# lands under "## Other" so no issue is ever dropped.
SEVERITIES = ("critical", "error", "warning", "info")
OTHER = "other"


def vault_path(vault=None):
    """An explicit vault, else the configured one (env or host.json), else None (jobs skip)."""
    return Path(vault) if vault else host.vault_path()


def frontmatter(text):
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    fm = {}
    for line in text[4:end].splitlines():
        if not line or line.startswith((" ", "\t", "-")) or ":" not in line:
            continue
        k, _, v = line.partition(":")
        v = v.strip()
        if not v:
            continue  # list header (e.g. "tags:") - lists are ignored
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        fm[k.strip()] = v
    body = text[end + 4:]
    return fm, body[1:] if body.startswith("\n") else body


def note_frontmatter(path):
    try:
        return frontmatter(Path(path).read_text(encoding="utf-8"))[0]
    except OSError:
        return {}


def board_columns(text):
    cols, cur = {}, None
    for line in text.splitlines():
        if line.startswith("## "):
            cur = line[3:].strip(); cols[cur] = []
        elif cur is not None and CARD_RE.match(line):
            cols[cur].append(line.lstrip("\t "))
    return cols


def _bold_span(line):
    """(start, end) of the first bold span whose ** markers are outside any backtick span."""
    in_tick = False
    start = None
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c == "`":
            in_tick = not in_tick
            i += 1
            continue
        if not in_tick and line[i:i + 2] == "**":
            if start is None:
                start = i + 2
            else:
                return start, i
            i += 2
            continue
        i += 1
    return None


def card_title(line):
    span = _bold_span(line)
    if span:
        t = line[span[0]:span[1]].strip()
        if t.startswith("~~"):
            t = t[2:].lstrip()
        if t.endswith("~~"):
            t = t[:-2].rstrip()
        return t
    t = CARD_RE.sub("", line).replace("~~", "")
    t = re.sub(r"·\s*@\{[^}]*\}", "", t)
    # The `📅 <date>` strip stays although due dates are retired (3.4): card_title also
    # runs over HISTORICAL board revisions from git (`board_at`), which still carry
    # hand-written due markers.
    t = re.sub(r"<!--.*?-->|\[\[[^\]]*\]\]|#\S+|📅\s*\d{4}-\d{2}-\d{2}", "", t)
    return t.strip(" ·")[:80]


# ---------------------------------------------------------------- health

def _default_runner(argv):
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout


def run_checker(vault, runner=None):
    root = host.get_or_default("vault.skill_root")      # the checker's checkout (host.json or env)
    if not root and runner is None:
        raise RuntimeError("no vault checker configured (vault.skill_root)")
    argv = ["uv", "run", "--directory", str(root or ""), "scripts/vault_health.py",
            "--path", str(vault), "--json"]
    return json.loads((runner or _default_runner)(argv))


def filter_copilot(issues):
    return [i for i in issues if not (i.get("files") and all(str(f).startswith("copilot/") for f in i["files"]))]


def severity_of(issue):
    """The bucket an issue is rendered under - unknown severities go to OTHER."""
    sev = str(issue.get("severity") or "").strip().lower()
    return sev if sev in SEVERITIES else OTHER


def severity_counts(issues):
    counts = {s: 0 for s in SEVERITIES + (OTHER,)}
    for i in issues:
        counts[severity_of(i)] += 1
    return counts


def health_summary(notes, issues):
    c = severity_counts(issues)
    parts = [f"error {c['error']}", f"warning {c['warning']}", f"info {c['info']}"]
    if c["critical"]:
        parts.append(f"critical {c['critical']}")
    if c[OTHER]:
        parts.append(f"other {c[OTHER]}")
    return f"health: {notes} notes, {len(issues)} issues ({', '.join(parts)})"


def prior_health(vault, today):
    best = None
    for p in sorted(Path(vault, "Knowledge").glob("Vault Health *.md")):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", p.name)
        if not m or m.group(1) >= today.isoformat():
            continue
        txt = p.read_text(encoding="utf-8", errors="replace")
        fm, body = frontmatter(txt)
        n = fm.get("total_issues")
        if n is None:
            mm = re.search(r"\*\*Issues:\*\*\s*(\d+)", body)
            n = mm.group(1) if mm else None
        if n is not None:
            best = (int(n), m.group(1))
    return best


def render_health(data, today, prior, committer_findings=None):
    issues = data.get("issues") or []
    total = len(issues); notes = data.get("total_notes", 0)
    out = ["---", f"date: {today.isoformat()}", "type: vault-health", "tags:", "  - vault-health", "ai-first: true",
           f"total_notes: {notes}", f"total_issues: {total}", "---", "## For future agent",
           f"Vault health report for {today.isoformat()}, written by maintenance/vault_jobs.py health (no model). "
           f"{total} issues across {notes} notes after dropping copilot/ scaffolding.", ""]
    for sev in SEVERITIES + (OTHER,):
        rows = [i for i in issues if severity_of(i) == sev]
        if not rows:
            continue
        out.append(f"## {sev.capitalize()}")
        out += [f"- {i.get('message', '?')} ({len(i.get('files') or [])} files)" for i in rows]
        out.append("")
    if committer_findings:
        out.append("## Machine committer (C3/M054)")
        out.append("A `maint(<job>)` commit touched a file the pipeline itself owns - a "
                    "job's write guard should have refused this. Read-only report; nothing "
                    "was blocked or reverted automatically.")
        out += [f"- {line}" for line in committer_findings]
        out.append("")
    out.append("## Trend")
    if prior is None:
        out.append("First health check - no prior trend.")
    else:
        k, d = prior
        word = "up from" if total > k else "down from" if total < k else "unchanged from"
        out.append(f"{total} issues, {word} {k} on {d}")
    return "\n".join(out) + "\n"


# C3/M054's vault-side half (row 127): the committer test in
# maintenance/tests/test_vault_git.py proves the shape of the rule on a fixture; this is
# the same rule run read-only against the real vault's history every night, so a
# violation surfaces in the health note instead of only in CI.
MACHINE_COMMITTER_GUARDED_PATHS = ("_CLAUDE.md", "references/")


def machine_committer_findings(vault, runner=None):
    """[str, ...] - one line per `maint(<job>)`-subject commit that ever touched
    `_CLAUDE.md` or `references/**`."""
    out = []
    for rel in MACHINE_COMMITTER_GUARDED_PATHS:
        for sha, subject in vault_git.machine_commits_touching(vault, rel, runner=runner):
            out.append(f"{sha[:12]} {subject!r} touched {rel}")
    return out


def health(vault=None, today=None, runner=None):
    vault = vault_path(vault); today = today or dt.date.today()
    data = run_checker(vault, runner)
    data["issues"] = filter_copilot(data.get("issues") or [])
    # Decision-log numbering (row 128, M069).
    try:
        import decision_log
        log = decision_log.find_log(vault)
        if log:
            rows = decision_log.parse(log.read_text(encoding="utf-8"))
            # + the generated index/views drift check (M070): a view naming a row
            # the table lost is reported exactly like a lint problem.
            for problem in (decision_log.lint(rows)
                            + decision_log._check_views(rows, Path(vault, "Decisions"))):
                # `info:` lines are informational only : the real log
                # has a permanent gap from row 119's renumber, and a warn that can never
                # be cleared teaches the reader to scroll past the duplicate/reuse
                # signal this check exists to carry.
                if decision_log.is_info(problem):
                    continue
                data["issues"].append({"severity": "warn", "check": "decision-log",
                                       "message": problem, "files": [log.name]})
    except Exception as exc:
        print(f"vault_jobs: decision-log lint skipped: {exc}", file=sys.stderr)
    note = Path(vault, "Knowledge", f"Vault Health {today.isoformat()}.md")
    note.parent.mkdir(parents=True, exist_ok=True)
    # NOT `runner`: that argument is the health-checker's own runner (run_checker's `uv
    # run vault_health.py` subprocess stub), a different shape entirely from vault_git's
    # git-command runner.
    committer_findings = machine_committer_findings(vault)
    note.write_text(render_health(data, today, prior_health(vault, today), committer_findings),
                    encoding="utf-8")
    summary = health_summary(data.get("total_notes", 0), data["issues"])
    if committer_findings:
        summary += f"; machine-committer: {len(committer_findings)} finding(s)"
    return summary


# ---------------------------------------------------------------- morning

# The pre-2c heading. Kept for exactly one reason (ruling 12): the last pre-2c morning
# run left this block in today's Daily note, and upsert_morning splices from here so the
# first post-2c run deletes it.
LEGACY_DUE_HEAD = "## 📋 Due today"
STALE_HEAD = "## ⚠️ Stale projects (7+ days idle)"
WORKLOG_HEAD = "## 💼 Work Log"


def render_template(text, today):
    text = text.replace('<% tp.date.now("YYYY-MM-DD") %>', today.isoformat())
    text = text.replace('<% tp.date.now("dddd") %>', today.strftime("%A"))
    return re.sub(r"<%[^%]*%>", "", text)


def last_activity(vault, path):
    fm = note_frontmatter(path)
    if fm.get("updated"):
        try:
            return dt.date.fromisoformat(fm["updated"][:10])
        except ValueError:
            pass
    try:
        out = subprocess.run(["git", "-C", str(vault), "log", "-1", "--format=%cd", "--date=short", "--",
                              str(Path(path).relative_to(vault))], capture_output=True, text=True, check=True).stdout.strip()
        return dt.date.fromisoformat(out) if out else None
    except (subprocess.CalledProcessError, ValueError, OSError):
        return None


def stale_projects(vault, today, days=7):
    """Active projects idle for `days`+, named by their path under Projects/."""
    root = Path(vault, "Projects")
    out = []
    for p in sorted(root.rglob("*.md")):
        if not p.is_file() or note_frontmatter(p).get("status") != "active":
            continue
        last = last_activity(vault, p)
        if last and (today - last).days >= days:
            out.append((p.relative_to(root).as_posix()[:-3], last))
    return out


def morning_sections(stale):
    """The morning note's one generated block. Due dates are retired (3.4):
    the generated board carries no `📅`, so a "Due today" section could only
    ever have rendered "None due today."."""
    b = [f"- [[Projects/{n}]] - last activity {d.isoformat()}" for n, d in stale] or ["None."]
    return f"{STALE_HEAD}\n\n" + "\n".join(b) + "\n"


EMPTY_ROWS = ("None.",)
# The only row shape morning_sections renders.
STALE_ROW_RE = re.compile(r"^- \[\[Projects/[^\]]*\]\] - last activity \d{4}-\d{2}-\d{2}$")


def _own_line(line):
    """True while the line still belongs to the rendered Stale block."""
    s = line.strip()
    return s == "" or s in EMPTY_ROWS or bool(STALE_ROW_RE.match(s))


def _block_end(note_text, anchor_idx):
    """Offset just past the block's own lines, starting after the anchor heading."""
    pos = note_text.find("\n", anchor_idx)
    if pos < 0:
        return len(note_text)
    pos += 1
    while pos < len(note_text):
        nl = note_text.find("\n", pos)
        line = note_text[pos:nl if nl >= 0 else len(note_text)]
        if not _own_line(line):
            return pos
        if nl < 0:
            return len(note_text)
        pos = nl + 1
    return len(note_text)


def upsert_morning(note_text, sections):
    # replace an existing block in place.
    if STALE_HEAD in note_text:
        stale_at = note_text.index(STALE_HEAD)
        start = note_text.index(LEGACY_DUE_HEAD) \
            if LEGACY_DUE_HEAD in note_text[:stale_at] else stale_at
        end = _block_end(note_text, stale_at)
        return note_text[:start] + sections + ("\n" if end < len(note_text) else "") + note_text[end:]
    # insert after the Work Log section's content
    if WORKLOG_HEAD in note_text:
        i = note_text.index(WORKLOG_HEAD) + len(WORKLOG_HEAD)
        nxt = [x for x in (note_text.find("\n## ", i), note_text.find("\n---", i)) if x >= 0]
        end = min(nxt) + 1 if nxt else len(note_text)
        return note_text[:end] + sections + "\n" + note_text[end:]
    return note_text.rstrip("\n") + "\n\n" + sections


def morning(vault=None, today=None):
    vault = vault_path(vault); today = today or dt.date.today()
    note = Path(vault, "Daily", f"{today.isoformat()}.md")
    created = not note.exists()
    if created:
        tpl = Path(vault, "Templates", "Daily Note.md")
        if not tpl.exists():
            # Spec §4: missing template -> exit 1, no traceback. run.sh stamps
            # the failure and Grafana's job-failed rule alerts.
            print(f"morning: template missing: {tpl}", file=sys.stderr)
            raise SystemExit(1)
        note.parent.mkdir(parents=True, exist_ok=True)
        text = render_template(tpl.read_text(encoding="utf-8"), today)
    else:
        text = note.read_text(encoding="utf-8")
    stale = stale_projects(vault, today)
    note.write_text(upsert_morning(text, morning_sections(stale)), encoding="utf-8")
    return f"morning: {'created' if created else 'updated'} Daily/{today.isoformat()}.md stale={len(stale)}"


# ---------------------------------------------------------------- digest

MAX_ITEMS = 6


def _git_out(vault, *args):
    try:
        return subprocess.run(["git", "-C", str(vault), *args], capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, OSError):
        return None


def nightly_commit(vault):
    out = _git_out(vault, "log", "-1", "--format=%H %ct", "--grep=^maint(nightly):")
    if not out or not out.strip():
        return None
    sha, ts = out.split()
    return sha, int(ts)


def board_at(vault, sha):
    return _git_out(vault, "show", f"{sha}:Boards/Engineering.md")


def changed_files(vault, sha):
    out = _git_out(vault, "show", "--format=", "--name-only", sha) or ""
    return [l for l in out.splitlines() if l.strip()]


def _join(items, empty):
    items = list(items)
    if not items:
        return empty
    if len(items) > MAX_ITEMS:
        return ", ".join(items[:MAX_ITEMS]) + f" +{len(items) - MAX_ITEMS} more"
    return ", ".join(items)


def standup(board_now, board_prev, today):
    cols = board_columns(board_now or "")
    titles = lambda col: [card_title(l) for l in cols.get(col, [])]
    prev_done = set(card_title(l) for l in board_columns(board_prev or "").get(DONE_COL, [])) if board_prev is not None else None
    moved = [t for t in titles(DONE_COL) if prev_done is not None and t not in prev_done]
    spec = titles(SPEC_READY_COL)
    return [f"  In Progress: {_join(titles(IN_PROGRESS_COL), 'clear')}",
            f"  Moved to Done since yesterday: {_join(moved, 'none')}",
            f"  Spec Ready (awaiting approval): {str(len(spec)) + ': ' + _join(spec, '') if spec else 'none'}",
            f"  Sprint / In Progress / Review: {len(cols.get(SPRINT_COL, []))} / {len(cols.get(IN_PROGRESS_COL, []))} / {len(cols.get(REVIEW_COL, []))}",
            f"  Sprint queue: {_join(titles(SPRINT_COL), 'empty')}",
            f"  Backlog depth: {sum(1 for l in cols.get(BACKLOG_COL, []) if UNTICKED_RE.match(l))}"]


def _notes(vault):
    for p in Path(vault).rglob("*.md"):
        rel = p.relative_to(vault).as_posix()
        if rel.startswith((".", "Templates/", "_trash/", "copilot/", "_export/")) or "/." in rel:
            continue
        yield p, rel


def _title(p, fm):
    return fm.get("title") or p.stem


def conflicts(vault):
    out = []
    for p, rel in _notes(vault):
        fm = note_frontmatter(p)
        if fm.get("type") == "conflict":
            out.append((_title(p, fm), rel[:-3]))
    return sorted(out)


def resurface(vault, today, exclude):
    cands = []
    for p, rel in _notes(vault):
        fm = note_frontmatter(p)
        if fm.get("type") not in ("idea", "speculation") or rel[:-3] in exclude:
            continue
        try:
            d = dt.date.fromisoformat(fm["updated"][:10]) if fm.get("updated") else dt.date.fromtimestamp(p.stat().st_mtime)
        except (ValueError, OSError):
            continue
        if (today - d).days >= 30:
            cands.append((d, _title(p, fm), rel[:-3]))
    if not cands:
        return None
    d, t, rel = min(cands)
    return t, rel, d.isoformat()


def retrieval_line(vault, today):
    p = Path(vault, "Knowledge", f"Retrieval Eval {today.strftime('%Y-%m')}.md")
    if not p.exists():
        return None
    fm = note_frontmatter(p)
    try:
        return f"  Retrieval: recall@5 {round(float(fm['recall-at-5']) * 100)}%, MRR {float(fm['mrr']):.2f}"
    except (KeyError, ValueError):
        return None


def read_excludes(comma_list="", path=None):
    """Resurfacing exclusions from --exclude (legacy) and --exclude-file."""
    out = [e.strip() for e in (comma_list or "").split(",")]
    if path:
        try:
            out += Path(path).read_text(encoding="utf-8").splitlines()
        except OSError:
            pass
    seen, uniq = set(), []
    for e in (x.strip() for x in out):
        if e and e != "(none yet)" and e not in seen:
            seen.add(e); uniq.append(e)
    return uniq


def digest(vault=None, today=None, exclude=(), now=None):
    vault = vault_path(vault); today = today or dt.date.today(); now = now or dt.datetime.now()
    board = Path(vault, "Boards", "Engineering.md")
    board_now = board.read_text(encoding="utf-8") if board.exists() else ""
    nc = nightly_commit(vault)
    prev = board_at(vault, nc[0]) if nc else None
    lines = ["BOARD STANDUP"] + standup(board_now, prev, today) + ["", "VAULT"]
    if nc and (now.timestamp() - nc[1]) < 36 * 3600:
        files = [f for f in changed_files(vault, nc[0]) if f.endswith(".md")]
        lines.append(f"  Overnight: nightly changed {len(files)} notes: {_join(files, 'nothing')}")
    else:
        lines.append("  Overnight: no run")
    needs = [f"conflict: {t} ({rel})" for t, rel in conflicts(vault)]
    needs += [f"{n} idle since {d.isoformat()}" for n, d in stale_projects(vault, today)]
    lines.append(f"  Needs you: {_join(needs, 'nothing')}")
    r = resurface(vault, today, list(exclude))
    if r:
        lines.append(f"  Resurfacing: {r[0]} ([[{r[1]}]]) - captured {r[2]}. Still true? Still worth pursuing?")
    rl = retrieval_line(vault, today)
    if rl:
        lines.append(rl)
    return "\n".join(lines) + "\n"


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="deterministic vault jobs")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("health", "morning", "digest"):
        s = sub.add_parser(name); s.add_argument("--vault", default=None)
        if name == "digest":
            s.add_argument("--exclude", default="")           # legacy comma list
            s.add_argument("--exclude-file", default=None)    # one path per line
    a = ap.parse_args(argv)
    if vault_path(a.vault) is None:
        print(SKIPPED)
        return 0
    if a.cmd == "health":
        print(health(a.vault))
    elif a.cmd == "morning":
        print(morning(a.vault))
    else:
        print(digest(a.vault, exclude=read_excludes(a.exclude, a.exclude_file)))


if __name__ == "__main__":
    main()
