"""`incidents landed`: a fix built outside the factory (tested blind, D161) is on main; record it on
the fix card so the closure gate can pass. `append_decision_row`: the `row <id> auto` helper."""
import contextlib
import fcntl
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from capsule import (db, incident_attach, incident_blind_run, incident_recurrence, incident_repo,
                     incident_watch, incidents, paths)


def _git(checkout, *args):
    try:
        res = subprocess.run(["git", "-C", str(Path(checkout).expanduser()), *args],
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"git {args[0]} failed in {checkout}: {exc}") from exc
    return res.returncode, res.stdout.strip()


def _full_sha(checkout, sha):
    rc, full = _git(checkout, "rev-parse", "--verify", "-q", f"{sha}^{{commit}}")
    if rc != 0 or not full:
        raise ValueError(f"commit {sha!r} not found in {checkout}")
    if _git(checkout, "merge-base", "--is-ancestor", full, "main")[0] != 0:
        raise ValueError(f"commit {full[:12]} is not an ancestor of main")
    return full


def _base(conn, inc, checkout, write=True):
    """The incident's base: `main_sha`, else the last main commit before it opened (stored when
    `write`)."""
    base = inc.get("main_sha")
    if not base:
        rc, base = _git(checkout, "rev-list", "-1", "--first-parent",
                        f"--before={int(inc['opened_at'])}", "main")
        if rc != 0 or not base:
            raise ValueError("no main commit before the incident opened")
        if write:
            conn.execute("UPDATE incidents SET main_sha=? WHERE id=?", (base, inc["id"]))
    return incident_repo.base_for(dict(inc, main_sha=base), checkout)


def _proofs(conn, inc, full, checkout, cheap=False, renamed_from=None):
    """The sha, base and recurrence-test proofs; an empty `main_sha` is set to the last main commit
    before the incident opened. `cheap`: read-only, and no novelty proof (the preflight)."""
    base = _base(conn, inc, checkout, write=not cheap)
    rc = _git(checkout, "merge-base", "--is-ancestor", full, base)[0]
    if rc != 1:      # 0: not after the base; 128: the base is unknown in this checkout
        raise ValueError(f"commit {full[:12]} is not after the incident's base {base[:12]}"
                         if rc == 0 else f"cannot compare with the incident's base {base[:12]}")
    test_id = inc.get("recurrence_test")
    if not test_id:
        raise ValueError(f"incident {inc['id']} has no recurrence test: set one with"
                         f" `capsule incidents retest {inc['id']} <test id>"
                         " --reason <why>` first")
    blind = incident_blind_run.BLIND_RE.match(test_id)
    if renamed_from and (blind or renamed_from == test_id
                         or not incident_recurrence.TEST_ID_RE.fullmatch(renamed_from)):
        raise ValueError(f"--renamed-from {renamed_from!r} must be another unittest or shell id"
                         f" than {test_id} (a blind id cannot be renamed)")
    if renamed_from and inc.get("closure") == "watch":
        raise ValueError("--renamed-from: a watch closure skips the novelty proof, so there is"
                         " no old id to read")
    if renamed_from and incident_recurrence.recurrence_test_is_new(renamed_from, checkout, base)[0]:
        raise ValueError(f"--renamed-from {renamed_from}: that test is still on main, so it was"
                         f" not renamed; name it with `--test {renamed_from}` instead")
    if not (cheap or blind or inc.get("closure") == "watch"):
        # a rename: the old id is the one the fix commit holds; the new id's own proof (new on
        # main since the base, and green there) is the gate's `recurrence_gaps`
        ok, detail = incident_recurrence.recurrence_test_is_new(renamed_from or test_id, checkout,
                                                                base, rev=full)
        if not ok:
            raise ValueError(f"commit {full[:12]} does not hold the fix: {detail}. Land the"
                             f" recurrence test {renamed_from or test_id} in the fix, or name the"
                             f" right test"
                             f" with `capsule incidents retest {inc['id']} <test id>"
                             " --reason <why>`")


def _action(cconn, card, board):
    """What the fix card needs: "move" (blocked, no runs: done plus a `completed` event), "event"
    (done by a person's close: a `completed` event only) or "none" (done with a live `completed`
    event). Another board's card must already be done there."""
    status, live = incidents._card_rows(cconn, card)
    if status == "done":
        return "none" if live else "event"
    if board is not None:
        raise ValueError(f"fix card {card} is {status or 'missing'} on board {board}: it must be"
                         " done there")
    why = incident_attach._replaceable(cconn, card)
    if why:
        raise ValueError(why)
    return "move"


def _write(cconn, card, action, full, author, now):
    with db.txn(cconn):      # a savepoint when cconn is the open incident connection
        if incidents._card_rows(cconn, card)[1]:
            return False      # another run landed it since the read: write nothing
        if action == "move":
            db.transition(cconn, card, "done", frm=("blocked",), claim=True, block_kind=None,
                          completed_at=now)
        db.event(cconn, card, "completed", {"commit": full, "outside": True, "author": author},
                 now=now)
    return True


def _incident(conn, incident_id, board, home):
    """(incident, fix card id, the other board's connection or None) for a landed fix card."""
    inc = incidents.get(conn, incident_id)
    if inc is None:
        raise LookupError(f"no incident {incident_id!r}")
    if inc["status"] in ("closed", incident_watch.SUPERSEDED):
        raise ValueError(f"incident {incident_id} is already {inc['status']}")
    if not inc.get("fix_card"):
        raise ValueError(f"incident {incident_id} has no fix card")
    target, card = incidents.split_fix_card(inc["fix_card"])
    if target in (None, board):
        return inc, card, None
    path = paths.board_db(target, home or paths.home())
    if not path.exists():
        raise ValueError(f"fix card {inc['fix_card']}: no board {target}")
    return inc, card, db.connect(path)


def preflight(conn, incident_id, test, *, checkout, full, board=None, home=None, blind_tests=None,
              renamed_from=None,
              timeout=incident_recurrence.TEST_TIMEOUT,
              run_test=incident_recurrence.run_recurrence_test,
              check_new=incident_recurrence.recurrence_test_is_new):
    """Run the close gate's recurrence-test proof for `test` (else the incident's own) with no
    lock held, after the cheap read-only refusals of `landed`. Returns the proven (test, base)
    pair; a failed proof raises ValueError."""
    inc, card, other = _incident(conn, incident_id, board, home)
    try:
        _action(other or conn, card, inc["fix_card"].split(":")[0] if other else None)
    finally:
        if other:
            other.close()
    inc = dict(inc, recurrence_test=test or inc["recurrence_test"])
    _proofs(conn, inc, full, checkout, cheap=True, renamed_from=renamed_from)
    pair = (inc["recurrence_test"], _base(conn, inc, checkout, write=False))
    gaps = incidents.recurrence_gaps(dict(inc, recurrence_test=pair[0], main_sha=pair[1]), checkout,
                                     run_test, timeout, check_new, blind_tests)
    if gaps:
        raise ValueError("; ".join(gaps))
    return pair


def landed(conn, incident_id, sha, *, author, board, checkout, home=None, now=None,
           proven=None, renamed_from=None, **gate):
    """Record that the incident's fix is on main at `sha`. Refuses unless the sha is on main, the
    incident has a recurrence test, and that test is new in the sha (a `watch` closure skips this
    check, as the close gate does; a blind id skips it here because nobody reads that test, and the
    close gate proves it red then green instead). The fix card, on this board or named
    `<board>:t_x`, is then: blocked with no runs -> done with a `completed` event; done by a
    person's close -> one `completed` event, no status change; done with a live `completed` event
    -> unchanged. A second run writes nothing. Another board gets only that one event, written
    before the incident evidence. An empty `main_sha` is set to the last main commit before the
    incident opened. Before any write, and before the write lock, it runs the close gate's
    recurrence-test proof (`preflight`, unless the caller passes its `proven` pair); a failure
    raises ValueError with the gate's text and changes nothing. `renamed_from` (an old test id):
    the incident's test was renamed after the fix, so the novelty proof reads the old id at `sha`
    and the evidence line names the rename. If the test or base changed since
    the proof, nothing is written. Returns the fix card value."""
    now = int(now if now is not None else time.time())
    full = _full_sha(checkout, sha)
    proven = proven or preflight(conn, incident_id, None, checkout=checkout, full=full,
                                 board=board, home=home, renamed_from=renamed_from, **gate)
    other = None
    try:
        with db.txn(conn):
            inc, card, other = _incident(conn, incident_id, board, home)
            cconn = other or conn
            action = _action(cconn, card, inc["fix_card"].split(":")[0] if other else None)
            _proofs(conn, inc, full, checkout, renamed_from=renamed_from)
            now_inc = incidents.get(conn, incident_id)
            if (now_inc["recurrence_test"], _base(conn, now_inc, checkout, write=False)) != proven:
                raise ValueError(f"incident {incident_id}: the recurrence test or base changed"
                                 " during the proof; nothing written, run landed again")
            # a card already done has no write; the evidence line still records the fix sha
            evidence = inc.get("evidence") or ""
            lines = []
            if (_write(cconn, card, action, full, author, now) if action != "none"
                    else full not in incident_repo.LANDED_RE.findall(evidence)):
                lines.append(f"fix landed outside the factory: card {inc['fix_card']}"
                             f" done at {full} by {author}")
            rename = f"recurrence test renamed after the fix: {renamed_from} -> {proven[0]}"
            if renamed_from and rename not in evidence:     # on every run that names it
                lines.append(rename)
            if lines:
                incidents.attach(conn, incident_id, lines)
    finally:
        if other:
            other.close()
    return inc["fix_card"]


ROW_RE = re.compile(r"^\|\s*(\d+)\s*\|", re.M)


def _new_row(inc, lines, decisions_note):
    """(row number, the draft line) for an incident that has no row in the note yet."""
    draft = (inc.get("decision_row_text") or "").strip()
    if "\n" in draft or inc["id"] not in draft or not re.match(r"^\|\s*<n>\s*\|", draft):
        raise ValueError(f"incident {inc['id']}: decision_row_text must be one line that starts"
                         f" `| <n> |` and names {inc['id']}")
    nums = [int(m.group(1)) for m in map(ROW_RE.match, lines) if m]
    if not nums:
        raise ValueError(f"no table rows in {decisions_note}")
    return max(nums) + 1, draft


def _write_note(note, lines):
    fd, tmp = tempfile.mkstemp(dir=note.parent, prefix=".row-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        os.chmod(tmp, note.stat().st_mode & 0o777)
        os.replace(tmp, note)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def _note_lock(note):
    """An exclusive flock on the note's folder, from the read to the replace. The lock leaves no
    file in the vault."""
    fd = os.open(note.parent, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _row_in_note(note, inc, decisions_note):
    """The incident's row number in the note, adding the draft row first when it has none."""
    lines = note.read_text(encoding="utf-8").split("\n")
    mine = re.compile(rf"^\|\s*(\d+)\s*\|.*{re.escape(inc['id'])}")
    row = next((int(m.group(1)) for m in map(mine.match, lines) if m), None)
    if row is None:
        row, draft = _new_row(inc, lines, decisions_note)
        last = max(i for i, ln in enumerate(lines) if ROW_RE.match(ln))
        lines.insert(last + 1, draft.replace("<n>", str(row), 1))
        _write_note(note, lines)
    return row


def append_decision_row(conn, incident_id, vault, decisions_note):
    """`incidents row <id> auto`: put the incident's draft decision row in the decisions note and
    set `decision_row`. A row already naming the incident is reused; else the draft's `<n>` becomes
    the highest row number + 1 and the line follows the last table row. Refuses, with no change,
    when the vault or note is missing or the draft is blank, has several lines, or does not name the
    incident. Returns the row number."""
    inc = incidents.get(conn, incident_id)
    if inc is None:
        raise LookupError(f"no incident {incident_id!r}")
    note = Path(vault or "/nonexistent-vault").expanduser() / decisions_note
    if not note.is_file():
        raise ValueError(f"decisions note not found: {decisions_note}")
    with _note_lock(note):
        row = _row_in_note(note, inc, decisions_note)
    incidents.set_decision_row(conn, incident_id, row)
    return row
