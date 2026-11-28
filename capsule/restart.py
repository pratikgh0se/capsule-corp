"""M155 (redesign D11, D12, spec 4.4): a card gets a Sonnet attempt with up to 2 fix rounds (3
review rounds), then ONE fresh run on `routing.implementer_restart` (Opus) with its failure notes
and up to 2 fix rounds of its own, then triage. Off unless `redesign.escalation`; with it off
every function here returns what the tick did before.

While the switch is on the ladder owns the stop limits it would otherwise trip:
`runaway.settings` and `failure_cap` lift the round, same-class and failure limits to the
ladder length, so the Sonnet streak cannot stop the card before the hand-off. The run and token
limits still apply (the restart is part of the card's budget, C1)."""
from capsule import config, outbox, prior_runs, review_scope, slack_msgs, stall_continue
from capsule.db import event, transition, txn

ROLE = "implementer_restart"
SONNET_ROUNDS = 3   # failed review rounds on the implementer route (attempt + 2 fixes)
OPUS_ROUNDS = 3     # the restart run + 2 fix rounds, all on the restart route
LADDER = SONNET_ROUNDS + OPUS_ROUNDS
KIND = "restart_failed"
_FAILED = "json_extract(payload,'$.reason') LIKE 'Review failed:%'"


def on(cfg):
    """The ladder is live: `redesign.escalation` is on and the restart route is configured."""
    return bool(config.redesign(cfg)["escalation"]) and ROLE in (cfg or {}).get("routing", {})


def failure_cap(cfg):
    """`caps.failure_limit` (3 when unset, as the landing paths read it) for any failure counted
    against the card: at least LADDER with the ladder on, so a review, crash, timeout or landing
    failure during the ladder goes back to ready, never to triage at once."""
    limit = ((cfg or {}).get("caps") or {}).get("failure_limit", 3)
    return max(limit, LADDER) if on(cfg) else limit


def _reset(conn, task_id):
    from capsule import runaway   # runaway.settings reads `on` and `LADDER`
    return runaway._reset(conn, task_id)


def _failed_rounds(conn, task_id):
    """Review rounds that failed since the card's last human reset: the ones `_verdict` recorded
    as a failure, plus the ones that tripped stop-the-line (a guide `continue` must not grant an
    extra run beyond the ladder)."""
    return conn.execute(
        f"SELECT COUNT(*) FROM task_events WHERE task_id=? AND id>? AND (kind='runaway' OR"
        f" (kind IN ('failed','failure_limit') AND {_FAILED}))",
        (task_id, _reset(conn, task_id)[0])).fetchone()[0]


def active(conn, cfg, task_id):
    """True when the card's next implementer run is on the restart route."""
    return on(cfg) and _failed_rounds(conn, task_id) >= SONNET_ROUNDS


def route(conn, cfg, task_id, role, run=None):
    """`config.route(cfg, role)`, except the implementer's route while the restart is due. `run`:
    the run row that exists at launch (its continue plan is the one made before it started)."""
    return config.route(cfg, _role(conn, cfg, task_id, role, run))


def _role(conn, cfg, task_id, role, run=None):
    """The restart route is due by the ladder, or a quiet-stop continue wants the stronger tier."""
    got = stall_continue.plan(conn, task_id, run) or {}
    strong = ROLE in (cfg or {}).get("routing", {}) and got.get("strong")
    return ROLE if role == "implementer" and (strong or active(conn, cfg, task_id)) else role


def round_for(conn, cfg, task_id):
    """The reviewer's `round` input for the card's next review round: counted from the restart
    while the restart attempt runs, so the reviewer.md round-4 rule (row 540, from round 4 only a
    pass, a decision or a safety finding) never turns the Opus attempt's review into a pass."""
    n = review_scope.reviewer_round(conn, task_id, _reset(conn, task_id)[1])
    return max(1, n - SONNET_ROUNDS) if active(conn, cfg, task_id) else n


def account(conn, cfg, task_id, role):
    """The account whose pause guard the dispatcher checks for this card's next run."""
    return cfg["routing"][_role(conn, cfg, task_id, role)]["account"]


def _visible(cfg, runs):
    """`runs` (prior runs) as the implementer may see them: with `redesign.blind_tests` = `gate`
    without a validator run, whose report quotes the rows of every blind test, passing ones too."""
    if config.redesign(cfg)["blind_tests"] != "gate":
        return runs
    return [r for r in runs if r.get("role") != "validator"]


def apply(conn, cfg, run, inputs, spool_root):
    """The implementer job `inputs` as the ladder wants them for a run on the restart route
    (`inputs` unchanged otherwise, switch off included). `prior_runs` is only runs of the Opus
    attempt, so none for the restart run itself. The restart run (the one right after the last
    Sonnet failure) also gets `restart_notes`, each failed round's note (findings and the
    validator's test output as `_verdict` commented them, trimmed to 3000 characters), oldest
    first, instead of `review_findings` (but a failing blind test's `blind:` finding stays, it is
    in no comment): a fresh dispatch, never a transcript."""
    task_id, floor = run["task_id"], 0
    since_at = _reset(conn, task_id)[1]
    if run["profile"] == "implementer" and active(conn, cfg, task_id):
        floor = conn.execute(
            "SELECT MIN(id) FROM task_runs WHERE task_id=? AND profile='implementer'"
            " AND model=? AND started_at>=?",
            (task_id, cfg["routing"][ROLE]["model"], since_at)).fetchone()[0] or run["id"]
    prior = prior_runs.paths_for(conn, spool_root, task_id, run["run_id"], after_id=floor)
    out = dict(inputs, prior_runs=_visible(cfg, prior))
    if floor and _failed_rounds(conn, task_id) == SONNET_ROUNDS:
        blind = [f for f in out.pop("review_findings", None) or [] if str(f).startswith("blind:")]
        if blind:       # a failing blind test's finding is in no card comment: it is kept
            out["review_findings"] = blind
        rows = conn.execute("SELECT body FROM task_comments WHERE task_id=? AND author='capsule'"
                            " AND body LIKE 'Review failed:%' AND created_at>=? ORDER BY id",
                            (task_id, since_at)).fetchall()
        out["restart_notes"] = [r[0][:3000] for r in rows]
    got = stall_continue.plan(conn, task_id, run) if run["profile"] == "implementer" else None
    if got and got.get("session_id"):            # a quiet stop's resume
        out.update(resume_session=got["session_id"], continue_prompt=got["prompt"])
    return out


def stop(conn, cfg, task, impl, now, text):
    """The failed round's stop-the-line check: the ladder's own last stop, then runaway's."""
    from capsule import runaway   # runaway imports this module
    return step(conn, cfg, task, impl, now, text) or runaway.step(conn, cfg, task, impl, now, text)


def step(conn, cfg, task, impl, now, text):
    """True when the round that just failed review was the ladder's last: the card goes to triage
    with a plain reason (`reset` is the only way out, a fresh Sonnet ladder). Called before the
    failure is recorded, so the count is the rounds before this one; `text` is this round's
    failure note."""
    if (not active(conn, cfg, task["id"]) or impl["model"] != cfg["routing"][ROLE]["model"]
            or _failed_rounds(conn, task["id"]) < LADDER - 1):
        return False
    why = (f"the Opus restart failed review too ({SONNET_ROUNDS} Sonnet rounds, then a fresh Opus"
           f" run and its fix rounds, {LADDER} in all); no further attempt, the card needs a person"
           f" to sort it. This round: {' '.join(text.split())}")[:2000]
    reply = f"python3 -m capsule human {cfg['board']} {task['id']} reset '<answer>'"
    with txn(conn):
        transition(conn, task["id"], "triage", frm=("review",), claim=True, block_kind=KIND,
                   block_recurrences=0)
        event(conn, task["id"], "blocked", {"reason": why, "kind": KIND, "recurrences": 1},
              run_pk=impl["id"], run_id=impl["run_id"], now=now)
        outbox.notify(conn, "blocked", f"{task['id']} triage ({KIND})", fields=[("reason", why)],
                      reply=reply, thread_key=task["id"], now=now,
                      key=slack_msgs.needs_key(task["id"]),
                      file_body=f"capsule: {task['id']} triage ({KIND}): "
                                f"{' '.join(why[:1500].split())} | reply: {reply}")
    return True
