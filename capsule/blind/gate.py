"""Gate mode, the consumer side (R32, blind spec 4.3): what the tick does with the blind rows the
validator was given (`blind.run`, `redesign.blind_tests` = `gate`). They are read back from the
validator run's own job.json (the evidence it was given, never recomputed), like the card check's.

- `failures`: one line per failing blind test group. The brief form (counts and ids only) is for
  everything the implementer's channel does not own: the failure comment, the events, the outbox.
  The full form (`full=True`) is the implementer's: one `review_findings.blind_finding` per failing
  test (id, given, expect, observed, nothing else).
- `hold`: a gate-mode `hold` row (no tests landed or they are stale, no usable repo, an
  inconclusive run, a skipped test), a failing row too malformed to read, or a job.json that
  cannot be read, holds the card with its plain reason and no finding. The hold offers `accept|
  reset`: a person's `accept` (author `owner`; the flag is self-declared) with a reason that
  names an open fix card waives it for the card's current acceptance text and hold reason, for
  one implementer run and 72 hours (Z4, `docs/ZENKAI.md`; one `override` event, no second
  waiver of the same text).
- `release`: tests landing for a card held for its blind tests send it back to review, no
  implementer round used, unless the card moved on since the hold (`AFTER_HOLD` events).
- `cap` (M110): the second verifier round that still has failing blind tests holds the card
  (`blocked`, cause `blind_cap:<acceptance digest>`) with one `decision.v1` for the owner instead of
  a third implementer round: each failing test's criterion (the card's acceptance statement it
  covers) beside its check (the scrubbed id, given, expect, observed, each cut short).
- `inputs`: the reviewer and the guide get counts and failing ids only, from this round's
  validator run."""
import hashlib
import json
import re
from pathlib import Path

from capsule import blocks, config, human, override, review_findings
from capsule.validator_report import exit_failed, is_blind
from capsule.db import event, txn

ROLES = ("reviewer", "guide")        # the review roles that get `inputs["blind"]`
OFFER = "accept|reset"               # what a person may do with a card held for its blind tests
CAUSE = "blind_gate"                 # a hold's `cause` is `blind_gate:<acceptance digest>:<reason>`
UNAVAILABLE = "blind tests unavailable"
NO_VERDICT = "blind tests gave no verdict"
FACTS = ("id", "given", "expect", "observed")   # a failing row's `test`: strings, all four
WAIVER_HOURS = 72                    # a waiver lasts this long at most (Z4, override rule 3)
AUTHORS = ("owner",)                  # who may waive a hold: a person (`--author`, self-declared)
FIX_CARD = re.compile(r"\bt_[0-9a-f]{8}\b")     # the waiver's fix card, named in the accept text
CAP = "blind_cap"                    # the cause of a card held by `cap`: `blind_cap:<digest>`
AFTER_HOLD = ("request_review", "failure_limit", "unblocked", "failure_reset")   # no release
# "run `r` came after event {0} (made at second {1})": by the order of its `claimed` event, not the
# whole-second clock (a run and an accept in one second); a run with no such event, by the clock
_AFTER = ("COALESCE((SELECT c.id>{0} FROM task_events c WHERE c.run_id=r.id AND c.kind='claimed'),"
          " r.started_at>={1})")
_AFTER_WAIVER = _AFTER.format("e.id", "e.created_at")    # run `r` after the override event `e`
_AFTER_ACCEPT = _AFTER.format("?", "?")                  # run `r` after the accept (id, second)


def on(cfg):
    """True when the board runs its blind tests in gate mode."""
    return config.redesign(cfg)["blind_tests"] == "gate"


def job_inputs(job_path):
    """The `inputs` object of a job.json: {} when the file cannot be read or its `inputs` is no
    object (a wrong-shape job file is never an error out of a tick pass)."""
    try:
        inputs = json.loads(Path(job_path).read_text()).get("inputs")
    except (OSError, ValueError, AttributeError):
        return {}
    return inputs if isinstance(inputs, dict) else {}


def outputs(spool_root, validator_run):
    """The `outputs` of the validator run's job.json: [] without a run or with no outputs in the
    file; None when the file cannot be read (missing, not JSON, not an object, outputs not a
    list): an unreadable file is not an empty one."""
    if validator_run is None:
        return []
    try:
        job = json.loads((Path(spool_root) / validator_run["run_id"] / "job.json").read_text())
        rows = ((job.get("inputs") or {}).get("outputs") or [])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return [o for o in rows if isinstance(o, dict)] if isinstance(rows, list) else None


def _valid(test):
    """True for a failing row's `test` with the four strings of a finding."""
    return isinstance(test, dict) and all(isinstance(test.get(k), str) for k in FACTS)


def _gate(rows):
    return [o for o in rows or [] if o.get("blind") == "gate"]


def failures(spool_root, validator_run, full=False):
    """[] unless a gate-mode blind test failed; else its findings (`full`) or one counts
    line (no test id). A failing row with no readable `test` is no finding (`hold` holds it)."""
    rows = _gate(outputs(spool_root, validator_run))
    failed = [o for o in rows if exit_failed(o) and _valid(o.get("test"))]
    if full:
        return [review_findings.blind_finding(o["test"]) for o in failed]
    return [f"blind tests: {len(failed)} of {len(rows)} failed"] if failed else []


def _reason(rows):
    """The plain reason a card is held on, or None when nothing holds it: `rows` None (the
    file was unreadable), a `hold` row's own reason, or a failing row that is no finding."""
    if rows is None:
        return UNAVAILABLE
    held = [o for o in rows if o.get("blind") == "hold"]
    if held:
        return str(held[0].get("reason") or NO_VERDICT)
    broken = [o for o in _gate(rows) if exit_failed(o) and not _valid(o.get("test"))]
    return UNAVAILABLE if broken else None


def _digest(task):
    """A short hash of the card's acceptance statements: what a waiver is for."""
    from capsule.blind import dispatch      # dispatch imports red, which imports this module
    text = "\n".join(dispatch.acceptance(task["body"]) or [])
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _accepted(conn, task_id, since):
    """(event id, time, text, author) of the latest applied `accept` after event `since`, when a
    person made it (`AUTHORS`, positively) and gave a reason; else None. An accept by the chief
    of staff (`cos`) or with no author on it waives nothing."""
    row = conn.execute(
        "SELECT id, created_at, payload FROM task_events WHERE task_id=? AND kind='intervention'"
        " AND id>? AND json_extract(payload,'$.kind')='accept' AND"
        " json_extract(payload,'$.outcome')='applied' ORDER BY id DESC LIMIT 1",
        (task_id, since)).fetchone()
    said = json.loads(row["payload"]) if row else {}
    text = str(said.get("text") or "").strip()
    who = human.canonical_author(said.get("author"))
    return (row["id"], row["created_at"], text, who) if text and who in AUTHORS else None


def _fix_card(conn, text, task_id):
    """The first card id in `text` that is an open task of this board other than the card itself
    (Z4, override rule 4: the waiver links a fix card), else None."""
    for found in FIX_CARD.findall(text):
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (found,)).fetchone()
        if row is not None and found != task_id and row["status"] not in ("done", "archived"):
            return found
    return None


def _seen(conn, task_id, kind, check, op, event_id):
    """True when the card has a `kind` event naming `check` whose id is `op` (`<` or `>`) than
    `event_id`."""
    row = conn.execute(f"SELECT 1 FROM task_events WHERE task_id=? AND kind=? AND id{op}? AND"
                       " json_extract(payload,'$.check')=?", (task_id, kind, event_id, check))
    return row.fetchone() is not None


def _refuse_once(conn, task_id, check, text, why, since, now):
    """An `override_refused` event (and its outbox line) once per accept; always False."""
    if not _seen(conn, task_id, "override_refused", check, ">", since):
        override._refuse(conn, task_id, check, text, why, now)
    return False


def _spent(conn, task_id, check, accept_id):
    """True when an earlier waiver of `check` (before event `accept_id`) was used: an implementer
    run started after it. One that no run followed (the card was held again by something else
    before it could pass) is not spent."""
    row = conn.execute(
        "SELECT 1 FROM task_events e WHERE e.task_id=? AND e.kind='override' AND e.id<? AND"
        " json_extract(e.payload,'$.check')=? AND EXISTS (SELECT 1 FROM task_runs r WHERE"
        f" r.task_id=e.task_id AND r.profile='implementer' AND {_AFTER_WAIVER})",
        (task_id, accept_id, check))
    return row.fetchone() is not None


def _waived(conn, cfg, task, cause, now):
    """True when a person's `accept` (`_accepted`) followed the latest hold of this card for the
    same acceptance text and reason (`cause`) and the waiver still stands (Z4, `docs/ZENKAI.md`
    override rules): it names an open fix card, it is under 72 hours old, no implementer run
    started after it, and no earlier waiver of this card's acceptance text was spent (a second one
    needs the text, the contract, to change: `amend`, then `reset`). The first time it is
    honoured it leaves one `override` event carrying the reason, the fix card, the issuer and
    the real expiry."""
    last = conn.execute(
        "SELECT id, json_extract(payload,'$.cause') FROM task_events WHERE task_id=? AND kind IN"
        " ('blocked','block_loop_detected') AND json_extract(payload,'$.cause') LIKE ?"
        " ORDER BY id DESC LIMIT 1", (task["id"], f"{CAUSE}:%")).fetchone()
    given = _accepted(conn, task["id"], last[0]) if last and last[1] == cause else None
    if given is None:
        return False
    check, expires = f"{CAUSE}:{cause.split(':')[1]}", given[1] + WAIVER_HOURS * 3600
    fix = _fix_card(conn, given[2], task["id"])
    ran = conn.execute("SELECT 1 FROM task_runs r WHERE r.task_id=? AND r.profile='implementer'"
                       f" AND {_AFTER_ACCEPT}", (task["id"], given[0], given[1])).fetchone()
    twice = _spent(conn, task["id"], check, given[0])
    if fix is None or twice:
        cmd = f"python3 -m capsule human {cfg['board']} {task['id']}"
        why = (f"the accept names no open fix card: send `{cmd} accept '<reason naming another"
               " open card of this board>'`" if fix is None else
               f"the acceptance text was already waived once ({check}); the ways on: the tests"
               f" land (the card releases itself), `{cmd} amend '<reason>' --body-file <path>`"
               f" then `{cmd} reset '<answer>'` so a new tester runs, or `{cmd} close '<why>'`")
        return _refuse_once(conn, task["id"], check, given[2], why, given[0], now)
    if now >= expires or ran is not None:
        return False
    if not _seen(conn, task["id"], "override", check, ">", given[0]):
        with txn(conn):
            event(conn, task["id"], "override", {
                "check": check, "reason": given[2][:500], "board": cfg["board"],
                "expires_at": expires, "fix_card": fix, "issuer": given[3]}, now=now)
    return True


def hold(conn, cfg, spool_root, task, validator_run, run_id, now, infra=(), infra_cause=None,
         failures=()):
    """True, and the card held (`needs_input`, offer `accept|reset`) with the plain reason, when
    gate mode's validator outputs hold it (`_reason`) and no person waived the hold (`_waived`).
    No finding goes to the implementer. `infra` (the round's review-infra lines) is recorded
    too, as the `blocked` event of kind `review_infra` a review-infra block would leave, and
    named in the hold's reason, so the incident is not lost. A round that has `failures` is the
    implementer's, except for `cap`'s second-round hold of failing blind tests."""
    if not on(cfg):
        return False
    if failures:     # a validator with no valid verdict is no failing blind round: infra route
        return not any(i.startswith("validator run") for i in infra) and cap(
            conn, cfg, spool_root, task, validator_run, run_id, now)
    reason = _reason(outputs(spool_root, validator_run))
    if reason is None:
        return False
    if reason == _pending() and validator_run is not None \
            and _landed_after_contract(conn, task["id"], validator_run["run_id"]) \
            and _fresh_round(conn, task["id"], now):
        return True     # the tests landed after this round's contract job began: not a hold
    cause = f"{CAUSE}:{_digest(task)}:{reason}"
    # a round that still has an infra failure never honours (so never spends) a waiver
    if not infra and _waived(conn, cfg, task, cause, now):
        return False
    text = "review run failed twice, no valid verdict: " + "; ".join(infra)[:1500]
    with txn(conn):
        if infra:
            event(conn, task["id"], "blocked", {
                "reason": text, "kind": "review_infra", "recurrences": 0,
                **({"cause": infra_cause} if infra_cause else {})}, run_id=run_id, now=now)
        blocks.block(conn, task["id"], kind="needs_input", run_id=run_id, now=now, offer=OFFER,
                     cause=cause, reason=reason + (f"\nAlso: {text}" if infra else ""))
    return True


def _prior_rounds(conn, spool_root, task_id, run):
    """How many earlier implementer rounds of the card (since its last reset) were followed by a
    validator run with gate-mode blind rows: its verifier rounds before `run`'s (a retried or
    redone validator run of one round counts once)."""
    since = conn.execute("SELECT COALESCE(MAX(created_at), 0) FROM task_events WHERE task_id=?"
                         " AND kind='failure_reset'", (task_id,)).fetchone()[0]
    runs = conn.execute(
        "SELECT v.run_id, (SELECT MAX(i.id) FROM task_runs i WHERE i.task_id=v.task_id AND"
        " i.profile='implementer' AND i.id<v.id) FROM task_runs v WHERE v.task_id=? AND"
        " v.profile='validator' AND v.started_at>=?", (task_id, since)).fetchall()
    now = next((i for r, i in runs if r == run["run_id"]), None)   # None: started before a reset
    return len({i for r, i in runs if i != now and _gate(outputs(spool_root, {"run_id": r}))})


def _criteria(conn, task):
    """{test id's last part: (statement number, its text)} from the card's latest tester report
    (`covered`) and its acceptance statements."""
    from capsule.blind import dispatch      # dispatch imports red, which imports this module
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND"
                       " kind='blind_tests_reported' ORDER BY id DESC LIMIT 1",
                       (task["id"],)).fetchone()
    texts, found = dispatch.acceptance(task["body"]), {}
    for entry in (json.loads(row[0] or "{}").get("covered") or []) if row else []:
        n = entry.get("statement")
        text = texts[n - 1] if 0 < n <= len(texts) else "(not on the card now)"
        for test in entry.get("tests") or []:
            found.setdefault(str(test).rsplit(".", 1)[-1], (n, text))
    return found


def _short(text, size=90):
    """`text` on one line, cut to `size` characters: no whole line of a test file survives."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= size else flat[:size - 3] + "..."


def _decision(task, impl, failed, criteria, cmd):
    """The `decision.v1` of a capped card: one entry per failing test, criterion beside check.
    `cmd` is `python3 -m capsule human <board> <card>`: the options name what to run."""
    shown, used = [], 0
    for test in failed:
        n, text = criteria.get(test["id"].rsplit(".", 1)[-1], (None, "(no criterion on record)"))
        block = (f"Criterion {n or '?'}: {_short(text, 200)}\n  Check {test['id']}\n"
                 + "\n".join(f"    {k}: {_short(test[k])}" for k in FACTS[1:]))
        if used + len(block) > 1800:
            shown.append(f"...and {len(failed) - len(shown)} more failing")
            break
        shown.append(block)
        used += len(block) + 1
    return {"schema": "decision.v1", "run_id": impl["run_id"], "task_id": task["id"],
            "claim_lock": impl["claim_lock"] or "capsule",
            "question": (f"{len(failed)} blind test(s) still fail in the second verifier round."
                         " An untestable criterion is usually a spec bug: fix it or close?"),
            "options": [
                {"id": "amend", "label": f"Amend the card's acceptance statements: `{cmd} amend"
                 " '<reason>' --body-file <path>`, then `reset '<answer>'` (a new tester runs;"
                 " the round count starts again)"},
                {"id": "close", "label": f"Close the card: `{cmd} close '<why>'`"}],
            "recommended": "amend", "context": "\n".join(shown)}


def cap(conn, cfg, spool_root, task, validator_run, run_id, now):
    """True, and the card held (a `spec_stale` block: `answer` and `unblock` are refused, the
    ways out are `amend` then `reset`, `accept`, or `close`; cause `blind_cap:<digest>`) with one
    `decision.v1` (`decision_requested` event and its outbox line), when this is the card's
    second verifier round and blind tests still fail: no third implementer round. Round 1 or a
    pass: False and nothing written. A held card is out of review, so a later tick raises no
    second decision. The caller (`hold`) has checked the gate switch."""
    if validator_run is None or dict(validator_run).get("error_class") == "invalid_output":
        return False        # no run, or one with no valid verdict (`invalid_output`): infra route
    failed = [o["test"] for o in _gate(outputs(spool_root, validator_run))
              if exit_failed(o) and _valid(o.get("test"))]
    if not failed or not _prior_rounds(conn, spool_root, task["id"], validator_run):
        return False
    from capsule import spool           # spool imports blocks, as this module does
    lock = conn.execute("SELECT claim_lock FROM task_runs WHERE run_id=?", (run_id,)).fetchone()
    cmd = f"python3 -m capsule human {cfg['board']} {task['id']}"
    doc = _decision(task, {"run_id": run_id, "claim_lock": lock and lock[0]}, failed,
                    _criteria(conn, task), cmd)
    with txn(conn):
        held = blocks.block(conn, task["id"], kind="spec_stale", run_id=run_id, now=now,
                            cause=f"{CAP}:{_digest(task)}",
                            reason=f"{len(failed)} blind test(s) still fail in round 2: see the"
                                   " decision (a criterion that cannot be tested is a spec bug)")
        if held:
            event(conn, task["id"], "decision_requested", doc, now=now)
            spool.decision_notify(conn, task["id"], doc, now, reply=f"{cmd} amend '<reason>'"
                                  " --body-file <path>, then reset '<answer>'; or close '<why>'")
    return bool(held)


def release(conn, task_id, now):
    """True when the card was held by `hold` and went back to review because its blind tests
    landed (`blocks.accept`: no implementer round used; one `blind_hold_released` event with the
    reason it was held on). A card waiting in review gets a fresh round (`_fresh_round`). False
    for a card that is not held by the gate."""
    row = conn.execute("SELECT status, block_kind FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is not None and row["status"] == "review":
        return _fresh_round(conn, task_id, now)
    if row is None or row["block_kind"] != "needs_input" or row["status"] not in (
            "blocked", "triage"):
        return False
    last = conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND kind IN"
                        " ('blocked','block_loop_detected') ORDER BY id DESC LIMIT 1",
                        (task_id,)).fetchone()
    held = json.loads(last["payload"]) if last else {}
    later = last is not None and conn.execute(
        f"SELECT 1 FROM task_events WHERE task_id=? AND id>? AND kind IN"
        f" ({','.join('?' * len(AFTER_HOLD))})", (task_id, last["id"], *AFTER_HOLD)).fetchone()
    if later or not str(held.get("cause") or "").startswith(f"{CAUSE}:") or not blocks.accept(
            conn, task_id, now=now):
        return False
    with txn(conn):
        event(conn, task_id, "blind_hold_released", {"reason": str(held.get("reason"))[:500]},
              now=now)
    return True


def _pending():
    from capsule.blind import run       # run imports this module
    return run.PENDING


def _landed_after_contract(conn, task_id, run_id):
    """True when a `blind_landed` event is newer than the `contract_started` event of validator
    run `run_id`: that round's contract job was submitted before the tests landed."""
    return bool(conn.execute(
        "SELECT 1 FROM task_events l WHERE l.task_id=? AND l.kind='blind_landed' AND l.id >"
        " (SELECT MAX(c.id) FROM task_events c WHERE c.task_id=? AND"
        " c.kind='contract_started' AND json_extract(c.payload,'$.run_id')=?)",
        (task_id, task_id, run_id)).fetchone())


def _fresh_round(conn, task_id, now):
    """A card in review whose round already has a validator run, computed before its blind tests
    landed (its gate result says "no blind tests yet"): one `request_review` event whose
    `after_run` is every run so far discards that round, so a fresh validator and contract round
    runs. No implementer round and no failure count. False when the round has no validator run."""
    if _round_validator(conn, task_id) is None:
        return False
    impl = conn.execute("SELECT id, run_id FROM task_runs WHERE task_id=? AND profile='implementer'"
                        " AND (outcome='completed' OR id IN (SELECT run_id FROM task_events WHERE"
                        " task_id=? AND kind='request_review')) ORDER BY id DESC LIMIT 1",
                        (task_id, task_id)).fetchone()
    if impl is None:
        return False
    after = conn.execute("SELECT MAX(id) FROM task_runs WHERE task_id=?", (task_id,)).fetchone()[0]
    with txn(conn):
        event(conn, task_id, "request_review", {"by": "blind_landed", "after_run": after},
              run_pk=impl["id"], run_id=impl["run_id"], now=now)
    return True


def scrubbed(doc):
    """`doc` (a validator document) with the excerpt and expected text of every `blind:` assertion
    removed, passing ones too: what the guide reads of the blind tests is counts and ids, never a
    test's given, expect or observed. Anything that is no document with assertions comes back
    unchanged."""
    if not isinstance(doc, dict) or not isinstance(doc.get("assertions"), list):
        return doc
    return {**doc, "assertions": [
        {k: v for k, v in a.items() if k not in ("excerpt", "expected")}
        if isinstance(a, dict) and is_blind(a.get("cmd")) else a
        for a in doc["assertions"]]}


def _round_validator(conn, task_id):
    """The newest validator run of the card's current review round (it started after the run
    under review, or after the last run a later `accept` of it saw), or None: an earlier round's
    counts are never a reviewer's."""
    floor = conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_runs WHERE task_id=? AND"
                         " profile='implementer'", (task_id,)).fetchone()[0]
    last = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind="
                        "'request_review' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    floor = max(floor, (json.loads(last[0] or "{}").get("after_run") or 0) if last else 0)
    return conn.execute("SELECT run_id FROM task_runs WHERE task_id=? AND profile='validator'"
                        " AND id>? ORDER BY id DESC LIMIT 1", (task_id, floor)).fetchone()


def inputs(conn, spool_root, task_id, role, cfg=None):
    """{"blind": {"counts": {pass, fail, error, skipped}, "failing": [test ids]}} for a reviewer
    or guide run of a card whose current round's validator run has gate-mode blind rows; else
    {}. With `cfg` in gate mode a card with no such rows (none landed, or none wanted) gets empty
    counts, which the prompt reads `acceptance: none` (M115)."""
    if role not in ROLES:
        return {}
    rows = _gate(outputs(spool_root, _round_validator(conn, task_id)))
    if not rows:
        return {"blind": {"counts": {}, "failing": []}} if cfg and on(cfg) else {}
    kinds = ("pass", "fail", "error", "skipped")
    counts = {k: sum(o.get("result") == k for o in rows) for k in kinds}
    return {"blind": {"counts": counts, "failing": [
        str(o.get("cmd", ""))[len("blind:"):] for o in rows if exit_failed(o)]}}
