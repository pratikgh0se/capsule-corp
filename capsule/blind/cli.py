"""`python3 -m capsule blind init|preflight <board>` (blind spec 2, R13). `init` creates a board's
blind test repo and its `blind_tests` config block; `preflight` says whether a blind run could start
for the board (read-only). Local only: nothing here creates a remote or touches the network.
`init` is idempotent and never overwrites an existing `blind.toml`, test or config block (a change
to a landed blind test is a risky card, D18)."""
import json
import os
import re
import stat
import sys
from pathlib import Path

from capsule import config, paths, safe_write
from capsule.blind import preflight

BOARD_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Wider than this is the operator's explicit `--may-read`: docs can name private paths.
DEFAULT_MAY_READ = ("README.md",)
# Adapters with an in-container harness runner today; the others are allowlisted in
# `config.BLIND_ADAPTERS` and refused here until their runner exists (Python first, spec 3.3).
BUILT_ADAPTERS = ("unittest",)
# Defined in preflight, which checks every blind.toml run line against them.
RUN_LINE, RUN_LINES = preflight.RUN_LINE, preflight.RUN_LINES

README = """# {board}-blind

Blind acceptance tests for the `{board}` board (decision rows 601/602, D15-D18).

Who may read this repo: the blind-tester role and the owner. Nobody else: not the implementer, the
reviewer, the guide or the chief of staff. The implementer's container never mounts it. Only results
and behaviour examples flow back, never test code.

Layout: `cards/<card_id>/` holds the tests, `examples.jsonl` and `HEADER.json`;
`interface/<card_id>/` is the snapshot of the public interface the tester saw; `blind.toml` names
the adapter and run command. The harness is not stored here; the tick copies it in at run time.

Landing and changing: the tick adds `cards/<card_id>/`. Changing or deleting a landed test is a
risky card that needs the owner's approval.
"""


def add_parser(sub):
    p = sub.add_parser("blind", help="blind validator tests: init a board's repo, preflight it")
    bsub = p.add_subparsers(dest="blind_cmd", required=True)
    init_p = bsub.add_parser("init", help="create the board's blind repo and config; local only")
    init_p.add_argument("board")
    init_p.add_argument("--repo", default=None, help="default: ~/CodeBase/<board>-blind")
    init_p.add_argument("--adapter", default="unittest", help=f"one of {config.BLIND_ADAPTERS}")
    init_p.add_argument("--may-read", action="append", default=None, metavar="GLOB",
                        help="a file the tester may see (repeatable); default README.md only")
    init_p.add_argument("--interface-command", action="append", default=[], metavar="CMD",
                        help="a command whose output is part of the tester's interface; repeatable")
    init_p.add_argument("--dry-run", action="store_true", help="print the plan; change nothing")
    pre_p = bsub.add_parser("preflight", help="exit 0 when a blind run could start, else 1 and why")
    pre_p.add_argument("board")
    for q in (init_p, pre_p):
        q.add_argument("--capsule-home", default=None)


def _display(repo):
    """`~/...` when the repo sits under the home directory, else the absolute path."""
    try:
        return "~/" + repo.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(repo)


def _toml(board, adapter):
    run, listing = RUN_LINES[adapter]
    return (f"version = 1\nboard = {json.dumps(board)}\nadapter = {json.dumps(adapter)}\n"
            f"run = {json.dumps(run)}\nlist = {json.dumps(listing)}\n")


def _create_repo(repo, board, adapter):
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "README.md").write_text(README.format(board=board), encoding="utf-8")
    (repo / preflight.TOML_NAME).write_text(_toml(board, adapter), encoding="utf-8")
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    for sub in ("cards", "interface"):
        (repo / sub).mkdir(exist_ok=True)
        (repo / sub / ".gitkeep").write_text("", encoding="utf-8")
    for args in (("init", "-q", "-b", "main"), ("add", "-A"),
                 ("commit", "-q", "-m", f"blind repo for board {board}")):
        done = preflight.git(repo, *args)
        if done.returncode:
            raise RuntimeError(f"git {args[0]} failed: {done.stderr.strip()}")


def _config_with_block(text, block):
    """`text` with a top-level `blind_tests` entry appended before the closing brace, so the rest
    of the live file keeps its formatting. Raises ValueError unless the result still validates."""
    body = text.rstrip()
    if not body.endswith("}"):
        raise ValueError("the board config does not end with '}'")
    new = f'{body[:-1].rstrip()},\n  "blind_tests": {json.dumps(block)}\n}}\n'
    try:
        config.validate(json.loads(new))
    except ValueError as exc:      # json.JSONDecodeError is a ValueError too
        raise ValueError(f"the board config would not validate: {exc}") from None
    return new


def _same_block(existing, block):
    want = config.blind_tests({"blind_tests": block})
    have = config.blind_tests({"blind_tests": existing})
    return (preflight.expand(have["repo"]) == preflight.expand(want["repo"])
            and {**have, "repo": ""} == {**want, "repo": ""})


def _load_board_config(board, home):
    cfg_path = paths.config_path(home, board)
    try:
        text = cfg_path.read_text(encoding="utf-8")
        cfg = json.loads(text)
        config.validate(cfg)
    except (OSError, ValueError) as exc:
        raise ValueError(f"board config {cfg_path}: {exc}") from None
    if cfg["board"] != board:
        raise ValueError(f"board config {cfg_path} is for board {cfg['board']!r}, not {board!r}")
    return cfg_path, text, cfg


def _plan_block(board, cfg, home, repo, adapter, may_read, interface_commands):
    """The repo path and the `blind_tests` block to write; ValueError when unusable."""
    if adapter not in config.BLIND_ADAPTERS:
        raise ValueError(f"adapter {adapter!r}: one of {config.BLIND_ADAPTERS}")
    if adapter not in BUILT_ADAPTERS:
        raise ValueError(f"adapter {adapter!r} is allowed but has no harness runner yet;"
                         f" built: {BUILT_ADAPTERS}")
    repo_path = Path(os.path.expanduser(repo or f"~/CodeBase/{board}-blind"))
    if not repo_path.is_absolute():
        raise ValueError(f"--repo {repo_path}: must be an absolute path")
    repo_path = preflight.expand(repo_path)
    block = {"repo": _display(repo_path), "adapter": adapter, "tester_may_read": list(may_read),
             "interface_commands": list(interface_commands)}
    bad = preflight.block_problems(block, preflight.worker_mounts(cfg, board, home))
    if bad:
        raise ValueError("; ".join(bad))
    return repo_path, block


def _ensure_repo(repo_path, board, adapter, dry_run):
    """Create the repo, or accept one that is already this board's; never touch what is in it."""
    if repo_path.exists() and any(repo_path.iterdir()):
        problems = preflight.toml_problems(repo_path / preflight.TOML_NAME, board, adapter)
        problems += [x for x in (preflight.repo_problem(repo_path),) if x]
        if problems:
            stale = any(preflight.NOT_SAFE in p for p in problems)
            head = (f"{repo_path}: the blind.toml run line is stale. " if stale else
                    f"{repo_path} exists and is not this board's blind repo: ")
            raise ValueError(head + "; ".join(problems))
        return f"blind repo: already present at {repo_path}, left as is"
    if not dry_run:
        _create_repo(repo_path, board, adapter)
    return f"blind repo: {'would create' if dry_run else 'created'} {repo_path}"


def _init_board(board, args, home):
    """Create the repo and the config block for `board`; returns the lines to print. Raises
    ValueError (plain words, nothing half-done) when it must refuse."""
    if not BOARD_NAME.match(board):
        raise ValueError(f"board name {board!r}: letters, digits, '.', '_' and '-' only")
    cfg_path, text, cfg = _load_board_config(board, home)
    repo_path, block = _plan_block(board, cfg, home, args.repo, args.adapter,
                                   args.may_read or DEFAULT_MAY_READ, args.interface_command)
    existing = cfg.get("blind_tests")
    if existing is not None and not _same_block(existing, block):
        raise ValueError(f"the board already has a different blind_tests block"
                         f" ({existing.get('repo')}); edit it by hand if you mean to change it")
    new_text = None if existing is not None else _config_with_block(text, block)
    lines = [_ensure_repo(repo_path, board, args.adapter, args.dry_run)]
    if new_text is None:
        lines.append(f"board config: blind_tests already set in {cfg_path}")
    else:
        lines.append(f"board config: {'would add' if args.dry_run else 'added'}"
                     f" blind_tests to {cfg_path}")
        if not args.dry_run:
            mode = stat.S_IMODE(cfg_path.stat().st_mode)
            safe_write.atomic_write(cfg_path.parent, cfg_path, new_text, mode=mode)
    lines.append("redesign.blind_tests stays as it is (off until you set report or gate)")
    return lines


def _preflight(args, home):
    try:
        cfg = config.load(paths.config_path(home, args.board))
    except (OSError, ValueError) as exc:
        print(f"capsule blind preflight: board config unreadable: {exc}", file=sys.stderr)
        return 1
    found = preflight.problems(cfg, args.board, preflight.worker_mounts(cfg, args.board, home))
    if found:
        print("blind: unavailable", file=sys.stderr)
        for line in found:
            print(f"  {line}", file=sys.stderr)
        return 1
    print("blind preflight ok")
    return 0


def handle(args, parser):    # signature matches every other DISPATCH entry; parser unused here
    home = paths.home(args.capsule_home)
    if args.blind_cmd == "preflight":
        return _preflight(args, home)
    try:
        lines = _init_board(args.board, args, home)
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"capsule blind init: {exc}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0
