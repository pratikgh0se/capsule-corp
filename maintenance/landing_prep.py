#!/usr/bin/env python3
"""Pack reviewed branches into one keeper batch (vault decision row 643: common maintenance is
scripted, never improvised). Replaces the hand squash script, marker and keeper brief.

Preview (no flag) changes nothing but remote-tracking refs: it fetches origin, then prints the
base (origin/main), each branch's changed files, the conflicts `git merge-tree` predicts, and the
brief it would write. `--apply` creates the worktree and branch keeper/batchN beside the
main checkout (never a name that exists), squash-merges every branch with its message file (the
intent / SHA256SUMS / __main__ conflicts the hand script resolved are unioned and reported as
"auto-unioned, unreviewed"; any other conflict stops and reports), checks each branch's own files
match the branch (files another branch, main or a union touched: every line the branch added must
still be there), appends intent rows if given (lint, re-pin that one file, `shasum -c`), then
writes the review marker `<out-dir>/<prefix>-review-ok` and the brief
`<out-dir>/keeper-brief-<prefix>.md` (neither is ever overwritten). It refuses when --approved
does not start with APPROVE, the worktree or branch exists, origin/main moves during the run, or a
squash leaves conflict markers. Last, once every other check passed, it fast-forwards local main
to origin/main (ff-only, clean main checkout; refuses naming the fix when main is behind and cannot
move), so a refusal never leaves main moved. Nothing is forced, rebased or deleted; every apply
step is a JSON line in landing_prep.jsonl under the maintenance state dir (events.state_root()).
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import events   # noqa: E402  state_root(): the one maintenance state dir

TEMPLATE = Path(__file__).resolve().parent / "templates" / "keeper_brief.md"
INTENT_DIR = "contracts/intent"
SUMS = f"{INTENT_DIR}/SHA256SUMS"
RESOLVABLE = (SUMS, "capsule/intent.py", "capsule/__main__.py",
              "capsule/tests/test_main_dispatch.py")
MARKER = "^(<{7} |>{7} |[|]{7}|={7}$)"      # any conflict-marker line (git grep -E)


class Refuse(Exception):
    """A rule the script will not break; main() prints it as one line and exits 1."""


def git(cwd, *args, check=True):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=300)
    if check and r.returncode != 0:
        raise Refuse(f"git {' '.join(args[:2])} failed: {r.stderr.strip()[:200]}")
    return r.stdout if check else r


def _main_checkout(start):
    """The MAIN checkout (parent of the common .git), also when run from a linked worktree."""
    common = Path(git(start, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    if common.name != ".git":
        raise Refuse(f"cannot resolve the main checkout from {start}; pass --repo")
    return str(common.parent)


def _union(text):
    """Keep both sides of every conflict block, ours first."""
    out, side = [], None
    for ln in text.splitlines(keepends=True):
        if ln.startswith("<<<<<<< "):
            side = "ours"
        elif ln.rstrip("\r\n") == "=======" and side:
            side = "theirs"
        elif ln.startswith(">>>>>>> ") and side:
            side = None
        else:
            out.append(ln)
    return "".join(out)


def _union_intent_py(text):
    """Union a conflict inside the STANDALONE tuple of capsule/intent.py, then re-sort it."""
    m = re.search(r"STANDALONE\s*=\s*\(\n", text)
    if not m or not (m.end() <= text.index("<<<<<<<")
                     and text.rindex(">>>>>>>") < text.index("\n)", m.end())):
        raise Refuse("capsule/intent.py conflict is outside STANDALONE")
    text = _union(text)
    m = re.search(r"STANDALONE\s*=\s*\(\n(.*?)\n\)", text, re.S)
    lines = [ln for ln in m.group(1).split("\n") if ln.strip()]
    names = sorted({ln.strip().strip(",").strip('"') for ln in lines})
    indent = re.match(r"\s*", lines[0]).group(0)
    body = "\n".join(f'{indent}"{n}",' for n in names)
    return text[: m.start(1)] + body + text[m.end(1):]


def _touched(repo, base, br):
    """(merge-base, files the branch changed since it left the base, files the base changed)."""
    mb = git(repo, "merge-base", base, br).strip()
    return (mb, git(repo, "diff", "--name-only", mb, br).split(),
            git(repo, "diff", "--name-only", mb, base).split())


def _dropped(wt, mb, br, f):
    """First non-blank line the branch added to f that HEAD's copy lacks, or None."""
    body = git(wt, "diff", mb, br, "--", f).partition("\n@@")[2]
    shown = git(wt, "show", f"HEAD:{f}", check=False)
    have = set(shown.stdout.splitlines()) if shown.returncode == 0 else set()
    return next((ln[1:] for ln in body.splitlines()
                 if ln.startswith("+") and ln[1:].strip() and ln[1:] not in have), None)


def _plan(a):
    """Check every rule that needs no write; return (base sha, worktree path, per-branch plan)."""
    repo = Path(a.repo)
    wt = repo.parent / f"{repo.name}-keeper-batch{a.batch}"
    if not a.approved.startswith("APPROVE"):
        raise Refuse("--approved must start with APPROVE")
    if len(a.branch) != len(a.msg) or not a.branch:
        raise Refuse("each --branch needs its own --msg (and at least one branch)")
    rows = (a.intent_rows, a.intent_file, a.intent_msg)
    if any(rows) and not all(rows) or not re.fullmatch(r"\w*", a.intent_file or ""):
        raise Refuse("--intent-rows, --intent-file (a plain name) and --intent-msg go together")
    for f in [*a.msg, *filter(None, [a.intent_rows, a.intent_msg])]:
        if not Path(f).is_file():
            raise Refuse(f"missing file: {f}")
    for f in (a.marker, a.brief):
        if f.exists():
            raise Refuse(f"would overwrite: {f}")
    keeper = f"keeper/batch{a.batch}"
    sha = git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{keeper}",
              check=False).stdout.strip()
    if wt.exists() or sha:
        raise Refuse(f"worktree or branch already exists: {wt} / {keeper}; by hand (not run): "
                     f"git worktree remove {wt}"
                     + (f"; git update-ref -d refs/heads/{keeper} {sha}" if sha else ""))
    a.subjects = [Path(m).read_text().strip().split("\n")[0][:100] for m in a.msg]
    git(repo, "fetch", "-q", "origin")
    base = git(repo, "rev-parse", "origin/main").strip()
    a.sync = _sync_main(a, base)
    shas = [git(repo, "rev-parse", f"{br}^{{commit}}").strip() for br in a.branch]
    return base, wt, [(br, sha, _touched(repo, base, sha)) for br, sha in zip(a.branch, shas)]


def _main_owner(repo):
    """Path of the checkout that has main checked out, or None."""
    path = None
    for ln in git(repo, "worktree", "list", "--porcelain").splitlines():
        if ln.startswith("worktree "):
            path = ln[9:]
        elif ln == "branch refs/heads/main":
            return path
    return None


def _sync_main(a, base):
    """Local main vs the just-fetched origin/main (a factory landing pushes origin only):
    (old sha, None) when it is behind and can fast-forward, (None, why) when it is behind and
    cannot, (None, None) when it is level or ahead."""
    repo = a.repo
    old = git(repo, "rev-parse", "--verify", "-q", "refs/heads/main", check=False).stdout.strip()
    if not old:
        return None, (f"there is no local main branch in {repo}; "
                      f"by hand (not run): git -C {repo} switch -c main origin/main")

    def ancestor(x, y):
        return git(repo, "merge-base", "--is-ancestor", x, y, check=False).returncode == 0
    if ancestor(base, old):
        return None, None
    head = f"local main is behind origin/main ({old[:8]} -> {base[:8]})"
    if not ancestor(old, base):
        return None, (f"local main has diverged from origin/main ({old[:8]} vs {base[:8]}); "
                      f"by hand (not run): git -C {repo} log --oneline origin/main..main, "
                      "then reconcile it before packing")
    cur = git(repo, "branch", "--show-current").strip()
    if cur != "main":
        other = _main_owner(repo)
        fix = f"by hand (not run): git -C {repo} switch main"
        if other:
            fix = (f"main is checked out in {other}; by hand (not run): "
                   f"git -C {other} merge --ff-only origin/main")
        return None, f"{head} but {repo} is on {('branch ' + cur) if cur else 'no branch'}; {fix}"
    if git(repo, "status", "--porcelain", "--untracked-files=no").strip():
        return None, (f"{head} and {repo} has uncommitted changes to tracked files; "
                      f"by hand (not run): git -C {repo} status, then commit or discard them")
    return old, None


def _render(a, wt, base, head, subjects):
    extra = f"Extra gates for this batch (same rules, own Bash calls): {a.extra_gates}\n"
    values = {"batch": a.batch, "prefix": a.prefix, "worktree": wt, "repo": a.repo,
              "out_dir": a.out_dir, "marker": a.marker, "base8": base[:8], "head8": head[:8],
              "commit_summary": "; ".join(subjects), "extra_gates": extra if a.extra_gates else ""}
    text = TEMPLATE.read_text()
    for key, val in values.items():
        text = text.replace("{{" + key + "}}", str(val))
    return text


def _preview(a, base, wt, plan):
    print(f"landing-prep: base origin/main {base[:8]}; would create {wt} on keeper/batch{a.batch}")
    old, why = a.sync
    if old:
        print(f"landing-prep: would fast-forward main {old[:8]} -> {base[:8]} in {a.repo}")
    if why:
        print(f"landing-prep: apply would refuse: {why}")
    seen = set()
    for (br, sha, (_, own, _)), subject in zip(plan, a.subjects):
        r = git(a.repo, "merge-tree", "--write-tree", "--name-only", base, sha, check=False)
        found = r.stdout.split("\n\n")[0].split("\n")[1:] if r.returncode == 1 else []
        flag = ", ".join(f"{c} ({'auto-union' if c in RESOLVABLE else 'WOULD STOP'})"
                         for c in found) or "none"
        if r.returncode > 1:
            flag = f"conflict check failed: {(r.stderr.strip().splitlines() or [''])[0]}"
        print(f"branch {br} ({sha[:8]}): {len(own)} files; conflicts: {flag}; message: {subject}")
        print("  " + "\n  ".join(own))
        if seen & set(own):
            print(f"  overlap with earlier branches (line check only): {sorted(seen & set(own))}")
        seen |= set(own)
    print(f"intent rows: {a.intent_rows or 'none'}; would write {a.marker}, {a.brief}")
    print("--- brief ---")
    print(_render(a, wt, base, "<head at apply>", a.subjects))
    print("landing-prep: preview, nothing changed; --apply creates it")


def _log(**row):
    path = events.state_root() / "landing_prep.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")


def _squash(wt, br, sha, msg, touched, claimed):
    """Squash the reviewed commit `sha` of branch br into the worktree and commit it; return
    (files it touched, files auto-unioned)."""
    mb, own, moved = touched
    before = git(wt, "rev-parse", "HEAD").strip()
    r = git(wt, "-c", "merge.conflictStyle=merge", "merge", "--squash", sha, check=False)
    conflicted = git(wt, "diff", "--name-only", "--diff-filter=U").split()
    if r.returncode != 0 and not conflicted:
        raise Refuse(f"squash of {br} failed: {r.stderr.strip()[:200]}")
    stop = [f for f in conflicted if f not in RESOLVABLE]
    if stop:
        raise Refuse(f"unexpected conflict squashing {br}: {', '.join(stop)}; worktree {wt} left")
    for f in conflicted:
        text = (wt / f).read_text()
        (wt / f).write_text(_union_intent_py(text) if f == "capsule/intent.py" else _union(text))
        git(wt, "add", f)
    staged = git(wt, "diff", "--cached", "--name-only").split()
    scan = git(wt, "grep", "-l", "-E", "-e", MARKER, "--", *staged, check=False)
    if scan.returncode != 1:        # grep: 1 = none found; 0 = found; anything else = an error
        why = "conflict markers left" if scan.returncode == 0 else "marker scan failed"
        raise Refuse(f"{why} ({br}) {scan.stderr.strip()[:100]}; worktree {wt} left")
    _sums_check(wt)
    git(wt, "commit", "-q", "-F", str(msg))
    skipped = [f for f in own if f in claimed | set(moved) | set(conflicted)]
    check = [f for f in own if f not in skipped]
    if check and git(wt, "diff", "--name-only", sha, "HEAD", "--", *check).strip():
        raise Refuse(f"{br}: its files differ from the branch after the squash; worktree {wt} left")
    for f in skipped:
        line = _dropped(wt, mb, sha, f)
        if line is not None:
            raise Refuse(f"{br}: {f} lost an added line {line[:80]!r}; worktree {wt} left")
    _log(what="squash", id=br, sha=sha, before=before,
         result=git(wt, "rev-parse", "HEAD").strip())
    return set(own), conflicted


def _sums_check(wt):
    """In every SHA256SUMS under the intent dir (lookout/ too): every pinned file matches, and
    every tracked file beside it is pinned."""
    files = [Path(f) for f in git(wt, "ls-files", "-z", "--", INTENT_DIR).split("\0") if f]
    if Path(SUMS) not in files:
        raise Refuse(f"{SUMS} is not tracked")
    for sums in sorted(f for f in files if f.name == "SHA256SUMS"):
        label = str(sums.relative_to(INTENT_DIR))
        r = subprocess.run(["shasum", "-a", "256", "-c", "SHA256SUMS", "--quiet"],
                           cwd=wt / sums.parent, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise Refuse(f"{label} check failed: {(r.stdout + r.stderr).strip()[:200]}")
        listed = {ln.split(None, 1)[1].lstrip("*") for ln in (wt / sums).read_text().splitlines()
                  if ln.strip()}
        beside = {f.name for f in files if f.parent == sums.parent}
        loose = sorted(beside - listed - {"SHA256SUMS"})
        if loose:
            raise Refuse(f"not pinned in {label}: {', '.join(loose)}")


def _add_rows(wt, a):
    """Append the rows to the one intent file, lint, re-pin only that file, verify, commit."""
    rows = [ln for ln in Path(a.intent_rows).read_text().splitlines() if ln.strip()]
    for ln in rows:
        json.loads(ln)
    name = f"{a.intent_file}.jsonl"
    target = wt / INTENT_DIR / name
    before = git(wt, "rev-parse", "HEAD").strip()
    with target.open("a") as f:
        f.write("\n".join(rows) + "\n")
    lint = subprocess.run(
        [sys.executable, "-c", "from capsule import intent; print(intent.lint_all())"],
        cwd=wt, env={"PYTHONPATH": str(wt), "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=120)
    if lint.stdout.strip() != "[]":
        raise Refuse(f"intent lint failed: {(lint.stdout + lint.stderr).strip()[:300]}")
    pin = f"{hashlib.sha256(target.read_bytes()).hexdigest()}  {name}"
    lines = (wt / SUMS).read_text().splitlines()
    at = next((i for i, ln in enumerate(lines) if ln.endswith(f"  {name}")), None)
    if at is None:      # a new file: keep the list sorted by name
        at = next((i for i, ln in enumerate(lines) if ln.split("  ")[-1] > name), len(lines))
        lines.insert(at, pin)
    else:
        lines[at] = pin
    (wt / SUMS).write_text("\n".join(lines) + "\n")
    _sums_check(wt)
    git(wt, "add", f"{INTENT_DIR}/{name}", SUMS)
    git(wt, "commit", "-q", "-F", a.intent_msg)
    _log(what="intent-rows", id=name, before=before, result=git(wt, "rev-parse", "HEAD").strip())


def _sync_apply(a, base, wt):
    """Fast-forward local main, the last step that can still refuse (so a refusal leaves it)."""
    old, repo = a.sync[0], Path(a.repo)
    if not old:
        return
    now, why = _sync_main(a, base)           # re-check: branch, dirty file, main moved
    if why or now != old:
        raise Refuse(f"{why or 'local main moved since planning'}; "
                     f"main not fast-forwarded; worktree {wt} left")
    git(repo, "merge", "--ff-only", "-q", base)
    _log(what="sync-main", id=str(repo), before=old, result=base)
    print(f"landing-prep: fast-forwarded main to {base[:8]} (was {old[:8]}) in {repo}")


def _apply(a, base, wt, plan):
    repo = Path(a.repo)
    if a.sync[1]:
        raise Refuse(a.sync[1])
    git(repo, "worktree", "add", "-q", "--no-track", "-b", f"keeper/batch{a.batch}", str(wt), base)
    _log(what="worktree-add", id=str(wt), before=base, result=f"keeper/batch{a.batch}")
    claimed, unioned = set(), set()
    for (br, sha, touched), msg in zip(plan, a.msg):
        own, union = _squash(wt, br, sha, msg, touched, claimed)
        claimed |= own
        unioned |= set(union)
    if a.intent_rows:
        _add_rows(wt, a)
    for br, sha, _ in plan:
        if git(repo, "rev-parse", f"{br}^{{commit}}").strip() != sha:
            raise Refuse(f"branch {br} moved during the run; worktree {wt} left, nothing written")
    git(repo, "fetch", "-q", "origin")
    if git(repo, "rev-parse", "origin/main").strip() != base:
        raise Refuse(f"origin/main moved during the run; worktree {wt} left, nothing written")
    _sync_apply(a, base, wt)
    head = git(wt, "rev-parse", "HEAD").strip()
    note = [f"auto-unioned, unreviewed: {', '.join(sorted(unioned))}"] if unioned else []
    brief = _render(a, wt, base, head, a.subjects + note)
    Path(a.out_dir).mkdir(parents=True, exist_ok=True)
    for path, text in ((a.brief, brief), (a.marker, a.approved + "\n")):   # marker = go, last
        with path.open("x") as f:
            f.write(text)
        _log(what="write", id=str(path), before=base, result=head)
    print(f"landing-prep: keeper/batch{a.batch} at {head[:8]} on base {base[:8]}; wrote "
          f"{a.marker}, {a.brief}")
    for line in note:
        print(f"landing-prep: {line}")


def main(argv=None):
    p = argparse.ArgumentParser(description="Pack reviewed branches into a keeper batch.")
    p.add_argument("--batch", type=int, required=True)
    p.add_argument("--prefix", required=True)
    p.add_argument("--branch", action="append", default=[])
    p.add_argument("--msg", action="append", default=[])
    p.add_argument("--intent-rows")
    p.add_argument("--intent-file")
    p.add_argument("--intent-msg", help="commit message file for the intent-rows commit")
    p.add_argument("--approved", required=True)
    p.add_argument("--extra-gates", default="")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--repo", help="default: the main checkout this script belongs to")
    p.add_argument("--apply", action="store_true")
    a = p.parse_args(argv)
    a.marker = Path(a.out_dir) / f"{a.prefix}-review-ok"
    a.brief = Path(a.out_dir) / f"keeper-brief-{a.prefix}.md"
    wt = None
    try:
        a.repo = a.repo or _main_checkout(Path(__file__).resolve().parent)
        base, wt, plan = _plan(a)
        _apply(a, base, wt, plan) if a.apply else _preview(a, base, wt, plan)
    except (Refuse, OSError, ValueError, subprocess.SubprocessError) as e:
        print(f"landing-prep: refused: {e}", file=sys.stderr)
        if a.apply and wt and wt.exists():
            _log(what="refused", id=str(wt), before="", result=str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
