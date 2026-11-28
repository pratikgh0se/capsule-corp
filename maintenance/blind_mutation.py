#!/usr/bin/env python3
"""Weekly blind-suite mutation score (plan unit R36, report only): how much of the project do a
board's blind tests actually catch? For each card in the blind repo it breaks the project in small
ways (swap a comparison, flip a True/False, swap an arithmetic operator, return None), runs the
card's blind tests against each broken copy and counts the mutants the tests kill.

Report: `<out_dir>/blind-mutation-<date>.json` (`blind_mutation.v1`), written atomically, replaced
if it runs twice in a day. It holds locations only (file, line, mutator): no test source, test
names, card file names or project source text. A card whose tests are not green on unmutated code
is not scored (`baseline` red, or error when the run itself broke); other cards still are. All
cards run one shared sample of mutants; `totals` counts each mutant once (killed if any card
kills it). Past 6 hours no new mutant starts and the report says `truncated: true`.

Safe by construction: every tree is a fresh `git archive` extraction in a temp dir that goes away
even when a run raises, `project` and `blind_repo` are only read, and one `run` happens at a time.
It writes to no board table, human file or outbox. The real `run` is the blind run's own code
(`capsule/blind/run.py`) in a network-off container labelled capsule.kind=test.

stdlib plus capsule's own modules. Exit 0 (a low score too), 2 for an unknown board or an
unreadable repo. `python3 maintenance/blind_mutation.py <board> [--max-mutants N]`."""
import argparse
import ast
import datetime
import json
import random
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from capsule import config, paths, safe_write  # noqa: E402
from capsule.blind import cli as blind_cli, preflight, run as blind_run  # noqa: E402
from capsule.runners import docker  # noqa: E402

SCHEMA = "blind_mutation.v1"
MAX_MUTANTS = 50
DEADLINE_SECONDS = 6 * 3600             # no new mutant starts after this
MARKER = ".blind-mutation-mount"          # proves the tree mount is not empty
PROBE_TIMEOUT = 30
RUN_TIMEOUT = 300                       # one card's blind tests against one tree
_COUNTS = ("tried", "killed", "survived", "inconclusive")
_CMP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.GtE: ast.Lt, ast.Gt: ast.LtE,
        ast.LtE: ast.Gt, ast.Is: ast.IsNot, ast.IsNot: ast.Is, ast.In: ast.NotIn,
        ast.NotIn: ast.In}
_ARITH = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.FloorDiv, ast.FloorDiv: ast.Mult}


def _node_sites(node):
    """[(mutator, line, apply)] for one ast node; `apply()` makes the change in its tree."""
    if isinstance(node, ast.Compare):
        return [("comparison_swap", node.lineno,
                 lambda o=node.ops, i=i, new=_CMP[type(op)]: o.__setitem__(i, new()))
                for i, op in enumerate(node.ops) if type(op) in _CMP]
    if isinstance(node, ast.BinOp) and type(node.op) in _ARITH:
        return [("arithmetic_swap", node.lineno,
                 lambda n=node, new=_ARITH[type(node.op)]: setattr(n, "op", new()))]
    if isinstance(node, ast.Constant) and type(node.value) is bool:
        return [("boolean_flip", node.lineno, lambda n=node: setattr(n, "value", not n.value))]
    if (isinstance(node, ast.Return) and node.value is not None
            and not (isinstance(node.value, ast.Constant) and node.value.value is None)):
        return [("return_none", node.lineno,
                 lambda n=node: setattr(n, "value", ast.Constant(None)))]
    return []


def _sites(tree):
    """Every place `tree` can be mutated, in a fixed walk order."""
    return [site for node in ast.walk(tree) for site in _node_sites(node)]


def _mutated(text, index):
    """`text` with its `index`-th site applied (the same walk order as `_sites`)."""
    tree = ast.parse(text)
    _sites(tree)[index][2]()
    return ast.unparse(tree)


def _is_test_path(rel):
    parts = rel.split("/")
    name = parts[-1]
    return "tests" in parts[:-1] or name.startswith("test_") or name.endswith("_test.py")


def _safe_member(member, dest):
    """The tar `data` filter, but a member it refuses (an absolute symlink, say) or whose name is
    not valid UTF-8 is left out instead of stopping the job."""
    try:
        member.name.encode("utf-8")
        return tarfile.data_filter(member, dest)
    except (tarfile.FilterError, UnicodeEncodeError):
        return None


def _extract(tar, dest):
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar) as archive:
        archive.extractall(dest, filter=_safe_member)


def _git(repo, *args):
    try:
        done = preflight.git(repo, *args)
    except (subprocess.TimeoutExpired, UnicodeDecodeError) as exc:
        raise RuntimeError(f"git {args[0]} failed in {repo}: {type(exc).__name__}") from None
    if done.returncode:
        raise RuntimeError(f"git {args[0]} failed in {repo}: {done.stderr.strip()[:200]}")
    return done.stdout


def _archive(repo, commit, tar, *subpaths):
    _git(repo, "archive", "--format=tar", "-o", str(tar), commit, *subpaths)


def _candidates(project, commit, tree):
    """Sorted [(file, line, mutator, index)] over the project's tracked non-test .py files, read
    from `tree` (an archive of `commit`). Invalid Python and non-UTF-8 files are skipped."""
    # no -z: git quotes a name with odd bytes, and a quoted name never ends in .py
    listing = _git(project, "ls-tree", "-r", commit).split("\n")
    found = []
    for entry in sorted(listing):
        meta, _, rel = entry.partition("\t")
        if not rel.endswith(".py") or meta[:6] not in ("100644", "100755") or _is_test_path(rel):
            continue
        try:
            sites = _sites(ast.parse((tree / rel).read_bytes().decode("utf-8")))
        except (SyntaxError, ValueError, RecursionError, OSError):
            continue          # invalid, not UTF-8, or left out of the archive (export-ignore)
        found += [(rel, line, mutator, i) for i, (mutator, line, _) in enumerate(sites)]
    return sorted(found)


def _verdict(run, tar, work, card_dir, edit=None):
    """`run` on a fresh tree, optionally with one file's source replaced: (file, new source)."""
    tree = Path(tempfile.mkdtemp(dir=work))      # a new directory each time, never a reused path
    try:
        _extract(tar, tree)
        if edit:
            (tree / edit[0]).write_text(edit[1], encoding="utf-8")
        return run(tree, card_dir)
    finally:
        shutil.rmtree(tree, ignore_errors=True)


def _empty():
    return {**dict.fromkeys(_COUNTS, 0), "score": None, "survivors": []}


def _tally(row, key, mutant):
    row["tried"] += 1
    row[key] += 1
    if key == "survived":
        row["survivors"].append({"file": mutant[0], "line": mutant[1], "mutator": mutant[2]})


def _key(result):
    return "killed" if result is False else "survived" if result is True else "inconclusive"


def _baselines(card_dirs, ctx):
    """({card: empty row, with `baseline` when not green}, [green cards]): every card's tests run
    once on the unmutated tree."""
    rows, green = {}, []
    for card, card_dir in card_dirs.items():
        rows[card] = _empty()
        base = _verdict(ctx["run"], ctx["tar"], ctx["work"], card_dir)
        if base is True:
            green.append(card)
        else:
            rows[card]["baseline"] = "red" if base is False else "error"
    return rows, green


def _run_mutants(rows, green, card_dirs, ctx):
    """Run the one shared sample against every green card. Returns (totals, truncated): each
    mutant counts once in the totals, killed if any card kills it, survived if none does and one
    ran to a result, else inconclusive. Past the deadline no new mutant starts."""
    totals = _empty()
    cands = ctx["candidates"] if green else []
    picks = random.Random(ctx["seed"]).sample(cands, max(0, min(ctx["max"], len(cands))))
    for mutant in picks:                  # sample order, so a deadline cuts a random part
        if time.monotonic() >= ctx["deadline"]:
            return totals, True
        source = _mutated((ctx["base"] / mutant[0]).read_text(encoding="utf-8"), mutant[3])
        results = {card: _verdict(ctx["run"], ctx["tar"], ctx["work"], card_dirs[card],
                                  (mutant[0], source)) for card in green}
        for card, result in results.items():
            _tally(rows[card], _key(result), mutant)
        seen = list(results.values())
        _tally(totals, "killed" if False in seen else _key(True if True in seen else None), mutant)
    return totals, False


def _ratio(killed, survived):
    return round(killed / (killed + survived), 4) if killed + survived else None


def _score_cards(work, names, repos, ctx):
    """({card: row}, totals, truncated) for the cards named in `names`; `repos` is
    (project, project commit, blind repo, blind commit)."""
    project, project_commit, blind_repo, blind_commit = repos
    _archive(blind_repo, blind_commit, work / "cards.tar", "cards")
    _extract(work / "cards.tar", work / "blind")
    _archive(project, project_commit, work / "project.tar")
    _extract(work / "project.tar", work / "base")
    ctx = {**ctx, "tar": work / "project.tar", "work": work, "base": work / "base",
           "candidates": _candidates(project, project_commit, work / "base")}
    card_dirs = {n: work / "blind" / "cards" / n for n in sorted(Path(x).name for x in names if x)}
    rows, green = _baselines(card_dirs, ctx)
    totals, truncated = _run_mutants(rows, green, card_dirs, ctx)
    for row in (*rows.values(), totals):
        row["score"] = _ratio(row["killed"], row["survived"])
    return rows, totals, truncated


def score(project, commit, blind_repo, out_dir, *, run, today, seed=0, max_mutants=MAX_MUTANTS,
          deadline_seconds=DEADLINE_SECONDS):
    """Score every card of `blind_repo` (at its HEAD) against `project` at `commit`; writes the
    report under `out_dir` and returns it. `run(tree, card_dir)` is True when all of a card's
    tests pass on `tree`, False when one fails, None when the run itself broke. All cards run
    one shared sample of at most `max_mutants`; past `deadline_seconds` no new mutant starts and
    the report says `truncated: true`."""
    project, blind_repo, out_dir = Path(project), Path(blind_repo), Path(out_dir)
    day = str(today)[:10]
    project_commit = _git(project, "rev-parse", "--verify", f"{commit}^{{commit}}").strip()
    blind_commit = _git(blind_repo, "rev-parse", "--verify", "HEAD^{commit}").strip()
    names = [n for n in _git(blind_repo, "ls-tree", "-d", "--name-only", blind_commit,
                             "cards/").split("\n") if n and not n.startswith('"')]
    ctx = {"run": run, "seed": seed, "max": max_mutants,
           "deadline": time.monotonic() + deadline_seconds}
    rows, totals, truncated = {}, _empty(), False
    with tempfile.TemporaryDirectory(prefix="blind-mutation-") as tmp:
        if any(names):
            repos = (project, project_commit, blind_repo, blind_commit)
            rows, totals, truncated = _score_cards(Path(tmp), names, repos, ctx)
    doc = {"schema": SCHEMA, "date": day, "project_commit": project_commit,
           "blind_commit": blind_commit, "truncated": truncated, "cards": rows, "totals": totals}
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_write.atomic_write(out_dir, out_dir / f"blind-mutation-{day}.json",
                            json.dumps(doc, indent=2) + "\n")
    return doc


class _TestRunner(docker.StrongRunner):
    """The strong container for a tree that is a bare archive, labelled capsule.kind=test and
    carrying no capsule-run-id: the factory's reaper, run counts and idle-gap check never see it,
    and the cleanup contract treats it as test debris. Its mount probe looks for the marker file
    the run wrote, since an archive has no .git for the stock probe to read."""

    def _d(self, *args, **kw):
        args = ["capsule.kind=test" if a == "capsule.kind=run" else a for a in args]
        for i, a in enumerate(args):
            if a.startswith("capsule-run-id=") and args[i - 1] == "--label":
                del args[i - 1:i + 1]
                break
        return super()._d(*args, **kw)

    def _probe(self, name, git_dir, requests):
        """(rc, output): zero only when the marker is visible, so an empty mount never starts."""
        try:
            return self._exec(name, f"test -e /workspace/{MARKER}", PROBE_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, f"{type(exc).__name__}: {exc}"


def _in_container(tmp, tree, blind_repo, blind_commit, card):
    """True when all of a card's blind tests pass on `tree`, False when one fails, None when the
    run broke or judged nothing (no `pass` row), through the blind run's own snapshot, run line
    and two-oracle parser."""
    snap, out = tmp / "blind", tmp / "blind-out"
    out.mkdir(mode=0o700)
    blind_run._snapshot(blind_repo, blind_commit, card, snap)
    line = blind_run._run_line(blind_repo, blind_commit, card)
    (tree / MARKER).write_text("")
    runner = _TestRunner()
    name = runner.start(uuid.uuid4().hex, str(tree), tmpdir=docker.CONTRACT_TMPDIR, ro=True,
                        mounts=(f"{snap}:/blind:ro", f"{out}:/out"), lifetime=RUN_TIMEOUT + 60)
    try:
        code, text = runner.exec(name, line, RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None
    finally:
        runner.stop(name)
    rows, problem = blind_run._parse(blind_run._read(out / blind_run.RESULTS_FILE), text, code)
    results = {r["result"] for r in rows}
    if problem or not results & {"pass", "fail", "error"}:
        return None
    return not results & {"fail", "error"}


def _docker_run(blind_repo, blind_commit):
    """The real `run(tree, card_dir)`: None whenever the run itself breaks."""
    def run(tree, card_dir):
        card = Path(card_dir).name
        if not blind_run.CARD_ID.match(card):
            return None
        try:
            with tempfile.TemporaryDirectory(prefix="blind-mutation-run-") as tmp:
                return _in_container(Path(tmp), tree, blind_repo, blind_commit, card)
        except Exception:        # docker gone, a bad snapshot: inconclusive, never a kill
            return None
    return run


def _say(text, code):
    print(text, file=sys.stderr if code else sys.stdout)
    return code


def _board_inputs(board):
    """((project, blind repo, report dir), None, None), or (None, message, code)."""
    home = paths.home()
    try:
        cfg = json.loads(paths.config_path(home, board).read_text(encoding="utf-8"))
        block = config.blind_tests(cfg)
    except (OSError, ValueError, AttributeError):
        return None, f"blind-mutation: board {board!r}: no readable config", 2
    redesign = config.redesign(cfg)
    if redesign["blind_tests"] == "off" or not (block or {}).get("repo"):
        return None, f"blind-mutation: {board}: skipped, blind tests off or no blind repo", 0
    repo = preflight.expand(block["repo"])
    project = preflight._local_repo_url(cfg.get("repo_url"))
    for path in (repo, project):
        if path is None or preflight.repo_problem(path):
            return None, f"blind-mutation: {board}: unreadable git repo {path}", 2
    out_dir = paths.board_dir(board, home) / "blind-mutation"
    return (project, repo, out_dir), None, None


def _terminate(signum, frame):
    """SIGTERM unwinds like an exit, so the temp copies and the container are cleaned up."""
    sys.exit(128 + signum)


def _run_board(board, max_mutants):
    """(report, report dir, None), or (None, None, (message, exit code)) with nothing to score."""
    found, message, code = _board_inputs(board)
    if found is None:
        return None, None, (message, code)
    project, repo, out_dir = found
    commit = _git(repo, "rev-parse", "--verify", "HEAD^{commit}").strip()
    doc = score(project, "HEAD", repo, out_dir, run=_docker_run(repo, commit),
                today=datetime.date.today(), max_mutants=max_mutants)
    return doc, out_dir, None


def main(argv):
    parser = argparse.ArgumentParser(prog="blind_mutation.py", description=__doc__.split("\n")[0])
    parser.add_argument("board")
    parser.add_argument("--max-mutants", type=int, default=MAX_MUTANTS, metavar="N")
    args = parser.parse_args(argv)
    if not blind_cli.BOARD_NAME.match(args.board):
        return _say(f"blind-mutation: board name {args.board!r} is not a plain name", 2)
    try:
        doc, out_dir, stop = _run_board(args.board, args.max_mutants)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:    # a git timeout too
        return _say(f"blind-mutation: {args.board}: {' '.join(str(exc).split())[:300]}", 2)
    if doc is None:
        return _say(*stop)
    totals, cards = doc["totals"], doc["cards"]
    if cards and all(c.get("baseline") == "error" for c in cards.values()):
        return _say(f"blind-mutation: {args.board}: no card's tests could run (docker down or"
                    f" the mount broken?); report in {out_dir}", 2)
    return _say(f"blind-mutation: {args.board}: score {totals['score']} (killed {totals['killed']},"
                f" survived {totals['survived']}, inconclusive {totals['inconclusive']});"
                f"{' truncated;' if doc['truncated'] else ''} report in {out_dir}", 0)


__all__ = (score.__name__, main.__name__)       # the two entry points other code may call

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _terminate)
    sys.exit(main(sys.argv[1:]))
