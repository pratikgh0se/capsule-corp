"""A blind run that cannot happen for a reason that holds for the whole board (blind tests repo
stale or missing, runner image missing, preflight refusal) hits every card the same way: one
`blind:unavailable` row each, which nobody reads in bulk. So the first card to hit such a reason
opens one `silent_failure` incident for (board, reason) and sends one outbox alert; every later
card on the board with the same reason, while that incident is open, adds itself to its evidence
(`card: <id>`, the count of those is the affected cards) and sends nothing. Another reason opens
its own incident. A reason about one card (`reason` is None) is the row alone. A repeat by the
incident rules is `incidents.open_incident`'s own REPEAT page, the only alert then. A board under
`pause:board` opens nothing. Incident text is the fixed words below and ids only: a preflight
problem names the blind repo's path and a runner refusal names a blind mount, neither is copied
(`incident_blind.masked` is the one spelling of a refusal)."""
import os
import re
import subprocess
import sys
import time

from capsule import dispatch, incident_blind, incidents, outbox
from capsule.db import txn
from capsule.runners import docker

# reason -> the words of the incident and the alert. Every reason here holds for the whole board.
BOARD_WIDE = {
    "no_block": "the board has no blind_tests block",
    "repo_missing": "the blind tests repo does not exist",
    "repo_not_git": "the blind tests repo is not a git repository with a commit",
    "mounts_overlap": "the blind tests repo overlaps a path a worker mounts",
    "toml_stale": "blind.toml of the blind tests repo is wrong or stale",
    "config": "the blind_tests block is invalid",
    "tester_may_read": "a tester_may_read glob is refused",
    "preflight": "the blind preflight refused the run",
    "blind_mount_refused": "the runner refused a blind mount",
    "runner_start_failed": "the blind runner container would not start (image missing?)",
    "runner_start_timeout": "docker run for the blind runner container timed out",
}
# words of a `preflight.problems` text -> its reason, first match wins
_PROBLEM_WORDS = (("has no blind_tests block", "no_block"), ("does not exist", "repo_missing"),
                  ("is not a git repository", "repo_not_git"), ("which a worker mounts",
                  "mounts_overlap"), ("blind.toml", "toml_stale"), ("blind_tests", "config"))
# `preflight.block_problems`' own glob refusal; "blind_tests.tester_may_read: ..." is `config`
GLOB_REFUSED = "tester_may_read '"
# docker's words (lower case) for an image or a daemon problem: these make `docker run`'s refusal
# the board's whatever paths it names; without them it is one card's only when it names just the
# card's own clone (see `report`)
DOCKER_DOWN = ("no such image", "unable to find image", "pull access denied", "manifest unknown",
               "cannot connect to the docker daemon", "is the docker daemon running")


def report(conn, cfg, card, run_id, problems=(), exc=None, card_paths=(), board_paths=()):
    """Open, or add `card` to, the board incident of the reason a blind run was unavailable: the
    first of its preflight `problems` (every one holds for the board), else the error `exc` it
    raised when that is a refused blind mount, a `docker run` that failed on an image or the
    daemon, or one that timed out. Any other error, or one that cannot be classified, is one
    card's: nothing. A `docker run` refusal is the board's unless it names the card's own clone
    (`card_paths`) and neither the blind folder mount (`board_paths`) nor the blind tests repo;
    docker's image and daemon words make it the board's whatever it names.
    Never raises (report mode must not stop a card); nothing for a paused board either."""
    try:
        reason = _reason(problems, exc, cfg, card_paths, board_paths)
        if reason:
            _report(conn, cfg["board"], card, run_id, reason, exc, int(time.time()))
    except Exception as err:      # the row is still returned: say why the incident is missing
        print(f"capsule blind unavailable: {type(err).__name__}", file=sys.stderr)


def _names(text, paths):
    """True when the lower-case `text` holds any of `paths` (as given or with symlinks resolved),
    not continuing into a longer name: a word character, "-" or ".word" after it means a
    different path (`<run>/blind` is not `<run>/blind-out`); ":" "," ")" "." quotes, "/" and the
    end all still name it."""
    return any(re.search(re.escape(p.lower()) + r"(?![\w-]|\.\w)", text)
               for path in paths if path for p in {str(path), os.path.realpath(str(path))})


def _reason(problems, exc, cfg, card_paths, board_paths=()):
    """The board-wide reason (a key of BOARD_WIDE), or None for one card's."""
    if problems:
        first = problems[0]
        return "tester_may_read" if first.startswith(GLOB_REFUSED) else next(
            (why for words, why in _PROBLEM_WORDS if words in first), "preflight")
    if incident_blind.refusal(exc):
        return "blind_mount_refused"
    if isinstance(exc, subprocess.CalledProcessError) and exc.cmd != docker._PROBE_CMD:
        said = f"{exc.stderr} {exc.output}".lower()      # str or bytes, either way text
        repo = (cfg.get("blind_tests") or {}).get("repo") or ""
        theirs = [*board_paths, repo, os.path.expanduser(repo)]
        card_only = _names(said, card_paths) and not _names(said, theirs)
        board = any(w in said for w in DOCKER_DOWN) or not card_only
        return "runner_start_failed" if board else None
    if isinstance(exc, subprocess.TimeoutExpired) and list(exc.cmd)[1:2] == ["run"]:
        return "runner_start_timeout"
    return None


def _report(conn, board, card, run_id, reason, exc, now):
    if dispatch.board_paused(conn, now):
        return
    key, role, mine = f"blind unavailable: {reason}", f"blind_unavailable:{board}", f"card: {card}"
    words = BOARD_WIDE[reason]
    with txn(conn):
        fp = incidents.fingerprint("silent_failure", key, role=role)
        live = conn.execute("SELECT id FROM incidents WHERE fingerprint=? AND status NOT IN"
                            " ('closed','superseded') AND COALESCE(closure,'') != 'watch'"
                            " ORDER BY opened_at LIMIT 1", (fp,)).fetchone()
        if live is not None:
            incidents.attach(conn, live["id"], [mine])
            return
        seen = [f"reason: {reason}", mine] + ([f"refusal: {incident_blind.masked(exc)}"]
                                              if reason == "blind_mount_refused" else [])
        inc, action = incidents.open_incident(conn, board=board, kind="silent_failure", key=key,
                                              evidence=seen, task_id=card, run_id=run_id,
                                              role=role, now=now)
        if action == "opened":       # a repeat already paged: that is the only alert
            outbox.notify(conn, "incident", f"blind tests unavailable on {board}: {words}",
                          fields=[("incident", inc), ("first card", card)], thread_key=board,
                          file_body=f"capsule: blind tests unavailable on {board} ({reason}):"
                                    f" {words}; first card {card}, incident {inc}", now=now)
