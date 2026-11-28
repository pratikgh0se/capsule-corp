"""Which boards and repos a host serves: `boards_served` and `registry`, read from
maintenance/repos.json, host.json and config.

Split out of `capsule/host.py` so that module keeps a small import closure (the sandbox runner
imports it through `capsule/runners/docker.py`); the `unblocked` fallback below is heavy.
`handle` wraps `host.handle` to answer `capsule host boards`.
"""
import json
import os
from pathlib import Path

from capsule import host, paths


def boards_served(home=None, repos_file=None):
    """Names of the boards this host serves, sorted: the `board` fields of maintenance/repos.json
    when that file exists (AGENT_OPS_REPOS_JSON overrides the path; tests), else config
    `unblocked.boards`, else capsule-corp. One rule for run.sh, the brief and `registry`."""
    path = repos_file or os.environ.get("AGENT_OPS_REPOS_JSON") or (
        paths.REPO / "maintenance" / "repos.json")
    try:
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
        return sorted({str(r["board"]) for r in rows if isinstance(r, dict) and r.get("board")})
    except (OSError, ValueError, TypeError):
        rows = None          # no usable registry file: the config rule below
    from capsule import unblocked   # lazy: a heavy module, only this fallback needs it
    return sorted(unblocked.configured_boards(home))


def registry(home=None, env=None):
    """The repo registry from host.json, in maintenance/repos.json's own shape: a list of
    {name, path, url, board} sorted by name. `url` is None without a `repo_urls` entry; `board`
    is the repo name when it is one of `boards_served`, else None. Bad or missing keys fall back
    to the public defaults (this is for scheduled jobs)."""
    repos = host.get_or_default("repos", home, env)
    urls = host.get_or_default("repo_urls", home, env)
    boards = boards_served(home)
    return [{"name": n, "path": repos[n], "url": urls.get(n), "board": n if n in boards else None}
            for n in sorted(repos)]


def handle(args, parser):
    """`capsule host ...`: `boards` here, every other verb in `host.handle`."""
    if args.host_cmd == "boards":
        print(" ".join(boards_served(args.capsule_home)))
        return 0
    return host.handle(args, parser)
