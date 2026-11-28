"""Was it already red? A contract command that fails on the card's head is re-run once on the
card's start commit, so the implementer and the maintainer can tell "the card broke it" from "the
branch was already broken when the card began".

The re-run happens in the contract job's own container (`check`, thread side: it extracts a clean
copy of `start_sha` from the read-only git dir into the container's tmpfs and runs the same command
there with the same timeout; no sqlite). The copy is a real local git checkout (`clone --shared`),
so a test that shells out to git behaves as on a normal checkout. Each judged row then carries
`base: {sha, result}` with result `red` (every failing test id of the head also fails on the start
commit; with no parseable ids, the exit codes both fail), `different` (the start commit fails with
other tests: `ids` lists up to 3), `pass`, or `unknown` (no start sha, a host (mac) job, the copy
could not be made, or the re-run timed out). `lines` turns the rows into failure text; `record`
opens one Korin incident per command when EVERY failing command is `red`. The card's pass or fail
is never changed here, with one exception: when the head command fails and the start commit
passes, the same command is re-run once on the head in the same container. If that re-run passes the
result is `flaky` (the validator gets the re-run row with exit 0 and a note naming the first
failing ids; `record` still opens one incident per failing id); if it fails again, the result stays
`pass` ("caused by this card"). One more exception: a `red` row whose failing test ids each have an
unclosed `base-flaky` incident opens no `base-red` incident; it calls `open_incident` with each
flaky key (a `fix_pending` incident recurs). One re-run only, the whole command, never test ids."""
import json
import re
import subprocess

from capsule import incidents
from capsule.blind import gate as blind_gate
from capsule.db import event, txn

SHORT = 12
HEAD_LINES = 5
IDS_SHOWN = 3
_ID = re.compile(r"^(?:(?:FAIL|ERROR): (.+?)|FAILED (?!\()(\S+)(?: - .*)?)\s*$", re.M)


class Verdict(str):
    """A result string that also carries the failing test ids seen on the start commit (for
    `flaky`: on the head) and, for `flaky`, the head re-run's (exit, output)."""
    ids = ()
    rerun = None


def failing_ids(out):
    """The failing test ids in unittest (`FAIL:`/`ERROR:`) or pytest (`FAILED <id>`) output."""
    return {a or b for a, b in _ID.findall(str(out or ""))}


def failed(row):
    """True for a row that really failed: non-zero and not a timeout (a timeout is no verdict)."""
    return row.get("exit") != 0 and not (
        row.get("exit") == 124 and str(row.get("output") or "").startswith("timed out"))


_KEY = re.compile(r"^(?:FAIL:|ERROR:|FAILED|Error|Traceback|AssertionError)")
_NOISE = ("ResourceWarning", "DeprecationWarning", "Enable tracemalloc")


def headline(out):
    """What to quote from a failing output: up to 3 failing test ids; else the first line that
    starts like a failure (warning noise skipped); else the first lines."""
    ids = sorted(failing_ids(out))[:IDS_SHOWN]
    if ids:
        return "failing tests:\n" + "\n".join(ids)
    text = [x.strip() for x in str(out or "").strip().splitlines()]
    for x in text:
        if _KEY.match(x) and not any(n in x for n in _NOISE):
            return x
    return "\n".join(text[:HEAD_LINES])


def check(run, done, base, git_dir, timeout, live=None):
    """{cmd: "red" | "different" | "pass" | "flaky" | "unknown"} for each failing row of `done`
    whose cmd is in `base[1]`, re-run once on `base[0]`, the start sha (None: unknown, nothing
    runs). `run` is the job's exec; `git_dir` is the container path of the clone's git dir.
    A start commit that passes gets one re-run of the same command on the head: a pass there is
    `flaky` (`rerun` is its (exit, output); `ids` are the head's first failing ids), a fail keeps
    `pass`."""
    sha, eligible = base
    todo = [c for c, rc, out, _ in done
            if str(c) in eligible and failed({"exit": rc, "output": out})]
    res = dict.fromkeys(todo, "unknown")
    if not todo or sha is None:
        return res
    d = f"/tmp/base-{sha[:SHORT]}"   # noqa: S108 - the container's own tmpfs
    try:   # a real checkout (shared objects, offline); no git or no clone: unknown, never fail
        ok = run(f"git -c safe.directory='*' clone -q --no-checkout --shared {git_dir} {d}"
                 f" && git -c safe.directory='*' -C {d} checkout -q {sha}", timeout)[0] == 0
    except subprocess.TimeoutExpired:
        ok = False
    head = {c: out for c, _, out, _ in done}
    for cmd in todo if ok else ():
        if live is not None:
            live.check()
        try:
            rc, out = run(f"cd {d} && ( {cmd}\n)", timeout)[:2]
        except subprocess.TimeoutExpired:
            continue
        v = Verdict("pass" if rc == 0 else "unknown" if rc == 124 else "red")
        want, got = failing_ids(head[cmd]), failing_ids(out)
        if v == "pass":
            if live is not None:
                live.check()
            try:
                again = run(cmd, timeout)[:2]
            except subprocess.TimeoutExpired:
                again = (124, "")
            if again[0] == 0:
                v = Verdict("flaky")
                v.rerun, v.ids = again, tuple(sorted(want))[:IDS_SHOWN]
                res[cmd] = v
                continue
        if v == "red" and want and not want <= got:
            v = Verdict("different")
        v.ids = tuple(sorted(got))[:IDS_SHOWN]
        res[cmd] = v
    return res


def rows(outs, results, sha):
    """`outs` with `base: {sha, result, ids}` on each row `check` judged. A `flaky` row (already
    the re-run row: the caller swapped it in) also gets a note naming the first failing ids."""
    out = []
    for o in outs:
        r = results.get(o["cmd"])
        if r is None:
            out.append(o)
            continue
        b = {"sha": sha, "result": str(r), "ids": list(getattr(r, "ids", ()))}
        if str(r) == "flaky":
            b["note"] = ("flaky: failed once on the head, passed on one re-run;"
                         f" first failing: {', '.join(b['ids']) or 'none parsed'}")
        out.append({**o, "base": b})
    return out


def settle(raw, results):
    """`raw` with each `flaky` command's (exit, output) replaced by its head re-run, so the exit,
    the output and the `check_expect` test all come from the re-run."""
    return [(c, *results[c].rerun, s) if results.get(c) == "flaky" else (c, rc, out, s)
            for c, rc, out, s in raw]


def flaky(outs):
    """The rows `rows` turned into a flaky re-run row."""
    return [o for o in outs if (o.get("base") or {}).get("result") == "flaky"]


def lines(spool_root, validator_run):
    """One note per failing row that has a base result, only under a failed validator verdict
    (the notes never fail a round by themselves). `unknown` says nothing; `flaky` says nothing
    either (no "caused by this card": its row carries the note)."""
    try:
        if json.loads(validator_run["handoff"] or "{}").get("verdict") != "fail":
            return []
    except (TypeError, ValueError, KeyError):
        return []
    out = []
    for o in blind_gate.outputs(spool_root, validator_run) or []:
        b = o.get("base") or {}
        if b.get("result") == "red":
            out.append(f"base: `{o['cmd']}` also fails on the start commit {b['sha'][:SHORT]}:"
                       " pre-existing, not caused by this card")
        elif b.get("result") == "different":
            out.append(f"base: `{o['cmd']}` also fails on the start commit {b['sha'][:SHORT]}"
                       f" but with different tests ({', '.join(b.get('ids') or ['none parsed'])}):"
                       " not proven pre-existing")
        elif b.get("result") == "pass":
            out.append(f"base: `{o['cmd']}` passes on the start commit {b['sha'][:SHORT]}:"
                       " caused by this card")
    return out


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:60]


def _flaky_key(cfg, text):
    return f"base-flaky: {cfg['board']}: {_slug(text)}"


def _known_flaky(conn, cfg, o):
    """The failing ids of row `o` when it has some and each has an unclosed flaky incident."""
    ids = sorted(failing_ids(o.get("output")))
    for tid in ids:
        fp = incidents.fingerprint("silent_failure", _flaky_key(cfg, tid))
        if conn.execute("SELECT 1 FROM incidents WHERE fingerprint=? AND status NOT IN"
                        " ('closed','superseded')", (fp,)).fetchone() is None:
            return []
    return ids


def record(conn, cfg, task, outs, now):
    """Open a silent_failure incident per failing command when every failing row is `red` on
    the start commit (the branch was already red), and one per failing test id of a flaky row
    (so a repeat counts). A red row whose failing ids each have an unclosed flaky incident opens
    no base-red incident: it calls open_incident with each flaky key (a fix_pending incident
    recurs). Returns the incident ids."""
    ids = []
    with txn(conn):
        for o in flaky(outs):
            event(conn, task["id"], "contract_flaky",
                  {"cmd": str(o["cmd"])[:300], "ids": o["base"]["ids"]}, now=now)
    for o in flaky(outs):
        for tid in o["base"]["ids"] or [None]:
            what = (f"`{o['cmd']}` failed once on the head of card {task['id']} and passed on one"
                    f" re-run: flaky{f' ({tid})' if tid else ''}")
            ids.append(incidents.open_incident(
                conn, board=cfg["board"], kind="silent_failure",
                key=_flaky_key(cfg, tid or o["cmd"]), evidence=[what],
                task_id=task["id"], now=now, description=what)[0])
    bad = [o for o in outs if failed(o)]
    if not bad or any((o.get("base") or {}).get("result") != "red" for o in bad):
        return ids
    for o in bad:
        known = _known_flaky(conn, cfg, o)
        if known:   # a known flaky id failed twice: it recurs on its flaky incident, no new one
            what = (f"`{o['cmd']}` fails on the start commit {o['base']['sha'][:SHORT]} of card"
                    f" {task['id']}: known flaky test id")
            ids += [incidents.open_incident(
                conn, board=cfg["board"], kind="silent_failure", key=_flaky_key(cfg, tid),
                evidence=[what], task_id=task["id"], now=now, description=what)[0]
                for tid in known]
            continue
        first = headline(o.get("output"))
        what = (f"`{o['cmd']}` fails on the start commit {o['base']['sha'][:SHORT]} of card"
                f" {task['id']}: the branch was already red")
        key = f"base-red: {cfg['board']}: {_slug(o['cmd'])}"
        ids.append(incidents.open_incident(
            conn, board=cfg["board"], kind="silent_failure", key=key,
            evidence=[what], task_id=task["id"], now=now,
            description=f"{what}\n{first}")[0])
    return ids
