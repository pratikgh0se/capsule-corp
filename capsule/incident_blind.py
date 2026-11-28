"""The blind-mount incident (R33, blind spec 4.4 items 1 and 8). `StrongRunner.start` raises
`RuntimeError("<check>: <mount path>")` when a started container mounts blind material; the tick
records it as a `tick_error`, and this module turns that event into a `review_infra` incident:
fingerprint = check name + card (the card rides in the role); the event text holds the path only
as `masked` (`path-<digest>`), never raw and never in the evidence list (Korin is handed those
paths as readable directories). The raw path goes to one stderr line, the tick's log.
`incidents._classify` calls `classify`; `Tick` starts the implementer's container through `start`,
which also holds the card. The contract container starts in a thread (`contract_jobs`), so
`ContractJobs.step` holds the card the same way on the tick thread."""
import hashlib
import os
import re
import subprocess
import sys

from capsule import blocks, docker_argv
from capsule.runners.docker import BLIND_CHECK, MissingImage

# `docker.py` ends the text with the run (`[run capsule-<id>]`): the tick writes its tick_error
# event and page only once per (card, text), so a repeat after a release must differ to count.
_LINE = re.compile(rf"RuntimeError: {re.escape(BLIND_CHECK)}: (.+?)(?: \[run [^\]]*\])?\Z",
                   re.DOTALL)


def mount_path(payload):
    """The mount path in a `tick_error` payload (the run's tag stripped), else None. In an event
    the tick wrote it is the masked `path-<digest>`, not the path."""
    hit = _LINE.search(str(payload.get("error", "")))
    return hit.group(1) if hit else None


_START_TIMEOUT = re.compile(
    r"RuntimeError: contract job failed: TimeoutExpired: Command \x27\[\x27docker\x27, \x27run\x27,"
    r" .* timed out after [\d.]+ seconds", re.DOTALL)


def contract_start_timeout(payload):
    """True when a `tick_error` payload (`classify`; `ContractJobs.step` builds the same text from
    the failure) is a contract job whose `docker run` timed out (a hung engine, inc_5d7beba0):
    board infra, not a crash. Its argv holds the image digest, so the incident key is fixed text;
    `docker exec`/`stop` timeouts and other errors stay crashes."""
    return bool(_START_TIMEOUT.search(str(payload.get("error", ""))))


def classify(row, payload):
    """(kind, role, key) of a `tick_error` event: the blind-mount incident, the contract
    `docker run` timeout (board infra), else the tick crash."""
    if mount_path(payload):
        return "review_infra", f"{BLIND_CHECK}:{row['task_id']}", BLIND_CHECK
    if contract_start_timeout(payload):
        return "silent_failure", "contract_start", "contract job failed: docker run timed out"
    return "crash", "tick", str(payload.get("error", "")).split(": ", 2)[-1]

def refusal(exc):
    """True when `exc` is the runner's blind-mount refusal: a `RuntimeError` whose message starts
    with `<check>: ` and names a path (what `StrongRunner.start` raises; a wrapped message or
    another error type quoting it is not one)."""
    return (isinstance(exc, RuntimeError) and str(exc).startswith(f"{BLIND_CHECK}: ")
            and bool(mount_path({"error": f"RuntimeError: {exc}"})))


def _digest(path):
    return hashlib.sha256(path.encode("utf-8", "surrogatepass")).hexdigest()[:12]


def roots(root, cfg):
    """The blind material's roots in a board: its blind repo as configured (`masked` adds the
    resolved path too) and the spool root `root`."""
    repo = ((cfg or {}).get("blind_tests") or {}).get("repo")
    return tuple(p for p in (repo and os.path.expanduser(repo), root and str(root)) if p)


def masked(exc, blind=()):
    """`exc` as text safe on a card that carries blind material, else None. A failed docker
    `run`/`create` (`CalledProcessError`, `TimeoutExpired`) keeps its text and the tail of docker's
    output with each mount's host source masked and each `blind` path (`roots`) as `path-<digest>`
    (`docker_argv.text`); the raw argv stays the caller's. Else the refusal: its path can sit
    under the blind repo or a blind spool, so only its digest stays (a repeat on another path is
    still another line); the run tag, a container name, stays so a repeat after a release is
    still a new one. Whatever else the message holds is dropped."""
    hide = {p: f"path-{_digest(p)}" for b in blind for p in {b, os.path.realpath(b)}}
    if (docker := docker_argv.text(exc, hide)) is not None:
        return docker
    if not refusal(exc):
        return None
    text = str(exc)[len(BLIND_CHECK) + 2:]
    tag = re.search(r" \[run (capsule-[0-9a-z]+)\]\Z", text)
    digest = _digest(text[:tag.start()] if tag else text)
    return f"{BLIND_CHECK}: path-{digest}" + (f" [run {tag[1]}]" if tag else "")


def logged(exc, card, blind=()):
    """`masked(exc)`; for a failed docker run its raw command first goes to one stderr line, the
    tick's log, and nowhere else."""
    text = masked(exc, blind)
    if text and isinstance(exc, subprocess.SubprocessError):
        print(f"capsule tick: docker failed: card {card}: {exc}", file=sys.stderr)
    return text


def why(exc, card=None, blind=()):
    """`Type: text` of a failure, as `masked` where it applies: what a board row may store. With
    a `card`, a failed docker run's raw command goes to the tick's log (`logged`)."""
    text = logged(exc, card, blind) if card else masked(exc, blind)
    return f"{type(exc).__name__}: {text or exc}"


def hold(conn, task_id, exc, run_id, now):
    """Hold the card (`needs_input`, offer `unblock|reset`) when `exc` is the runner's blind-mount
    refusal (`refusal`); a `MissingImage` holds it as `capability` instead (no failure, no page;
    release it once the image is built). True when held. Without the hold the lease reclaim, or
    the next review pass, would start the container again, and nobody would see it."""
    if isinstance(exc, MissingImage):
        blocks.block(conn, task_id, kind="capability", reason=str(exc), run_id=run_id, now=now,
                     offer="unblock|reset")
        return True
    if not refusal(exc):
        return False
    blocks.block(conn, task_id, kind="needs_input", reason=masked(exc), run_id=run_id, now=now,
                 offer="unblock|reset")
    return True


def start(conn, runner, task, now, run_id, ws, **kw):
    """`runner.start(run_id, ws, **kw)` for the implementer's container, holding the card on a
    blind-mount refusal (`hold`). The error is re-raised: the tick records it and the incident
    follows. The contract container starts in a thread, so `ContractJobs.step` calls `hold`."""
    try:
        return runner.start(run_id, str(ws), **kw)
    except RuntimeError as exc:
        hold(conn, task["id"], exc, run_id, now)
        raise
