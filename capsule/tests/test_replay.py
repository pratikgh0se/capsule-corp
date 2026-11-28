import contextlib, hashlib, io, json, sqlite3, tempfile, unittest
from pathlib import Path
from unittest import mock
from capsule import blocks, config, db, dispatch, lease, paths, replay, seats

CFG = config.load(paths.REPO / "capsule" / "config.example.json")
CFG_FILE = str(paths.REPO / "capsule" / "config.example.json")
EPIC = CFG["epics"][0]
CFG2 = dict(CFG, epics=[EPIC, "epic/other"], caps=dict(CFG["caps"], max_running=2))
OFF = dict(CFG, redesign={})


def _board(tc, name):
    path = Path(tempfile.mkdtemp()) / name
    db.create_board(path)
    conn = db.connect(path)
    tc.addCleanup(conn.close)
    return path, conn


def _finish(conn, ids, runs, now):
    """The workers that started 20 s ago are done: review, then done."""
    for name, (run_pk, started) in list(runs.items()):
        if now - started >= 20:
            lease.end_run(conn, run_pk, status="done", outcome="completed", now=now)
            with db.txn(conn):
                db.transition(conn, ids[name], "review", claim=True)
                db.event(conn, ids[name], "request_review", None, run_pk=run_pk, now=now)
                db.transition(conn, ids[name], "done", completed_at=now)
                db.event(conn, ids[name], "completed", {"commit": "x"}, run_pk=run_pk, now=now)
            del runs[name]


def history(tc, cfg):
    """A board file whose claims were made by the real `dispatch.ready` and `lease.claim`, one
    tick every 10 s: cards of mixed priority across the epics, a child that waits for its parent,
    runs that finish two ticks after they start. Returns (path, ids by name, claim order)."""
    path, conn = _board(tc, "k.db")
    epics, ids, order, runs = config.epic_branches(cfg), {}, [], {}
    spec = [("a", 0, 0), ("b", 5, 0), ("c", 0, 1), ("d", 9, 3), ("f", 2, 3), ("g", 0, 6)]
    for name, prio, tick in spec:
        ids[name] = db.new_task(conn, title=name, assignee="dev-frontend",
                                branch=epics[tick % len(epics)], now=1000 + tick * 10 - 5)
        conn.execute("UPDATE tasks SET priority=? WHERE id=?", (prio, ids[name]))
    ids["e"] = db.new_task(conn, title="e", assignee="dev-frontend", branch=epics[0],
                           status="todo", parents=[ids["a"]], now=1001)
    for tick in range(20):
        now = 1000 + tick * 10
        _finish(conn, ids, runs, now)
        blocks.promote_ready(conn, now=now)
        while dispatch.ready(conn, cfg, {"strong"}, now):
            card = dispatch.ready(conn, cfg, {"strong"}, now)[0]["id"]
            got = lease.claim(conn, card, ttl=900, role="implementer", now=now)
            name = next(k for k, v in ids.items() if v == card)
            runs[name] = (got.run_pk, now)
            order.append(card)
    return path, ids, order


class Cards:
    """The cards of `kinds_board`: `want` is the status each must rebuild to."""

    def __init__(self, conn):
        self.conn, self.ids, self.want, self.t = conn, {}, {}, 1000

    def card(self, name, status, expect, parents=()):
        self.t += 10
        self.ids[name] = db.new_task(
            self.conn, title=name, assignee="dev-frontend", branch=EPIC, status=status,
            parents=[self.ids[p] for p in parents], now=self.t)
        self.conn.execute("UPDATE tasks SET priority=-1 WHERE id=?", (self.ids[name],))
        self.want[name] = expect

    def log(self, name, kind, payload=None, at=0):
        """A kind the current code no longer writes (or never wrote): straight into the log."""
        self.conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
            (self.ids[name], kind, json.dumps(payload) if payload else None, self.t + at))

    def move(self, name, to, kind, payload=None):
        self.t += 10
        db.transition(self.conn, self.ids[name], to, claim=True)
        self.log(name, kind, payload)

    def force(self, name, status, **cols):
        sets = "".join(f", {c}=?" for c in cols)
        self.conn.execute(f"UPDATE tasks SET status=?{sets} WHERE id=?",
                          (status, *cols.values(), self.ids[name]))


def _status_cards(b):
    b.card("human_parked", "ready", "blocked")
    b.move("human_parked", "blocked", "parked", {"by": "human"})
    b.card("legacy_parked", "ready", "parked")
    b.t += 10
    b.force("legacy_parked", "parked")                  # the old dispatcher's own status
    b.log("legacy_parked", "parked", {"by": "owner"})
    b.card("gave_up", "ready", "blocked")
    b.move("gave_up", "blocked", "gave_up", {"failures": 3})
    b.card("scheduled", "ready", "scheduled")
    b.move("scheduled", "scheduled", "scheduled")
    b.card("korin", "todo", "done")
    b.move("korin", "done", "korin_applied", {"incident": "inc_1"})
    b.card("runaway", "ready", "triage")
    b.move("runaway", "triage", "runaway", {"trigger": "t"})
    b.card("contradiction", "ready", "triage")
    b.move("contradiction", "triage", "contradiction", {"routed": "guide"})
    b.card("restart", "ready", "triage")
    b.move("restart", "triage", "blocked", {"kind": "restart_failed"})
    b.card("by_hand", "ready", "done")                  # blocked, then finished with no event
    b.move("by_hand", "blocked", "blocked", {"kind": "needs_input"})
    b.force("by_hand", "done", completed_at=b.t + 5)


def _flow_cards(b):
    b.card("timed_out", "ready", "ready")
    b.log("timed_out", "timed_out", {"role": "implementer"}, 1)      # moves nothing itself
    b.card("intake", "triage", "ready")                 # filed `triage` by intake, then reset
    b.log("intake", "created", {"by": "intake", "intake_id": "rq_1"}, 1)
    b.t += 10
    blocks.reset_failures(b.conn, b.ids["intake"], by="human", now=b.t)
    b.card("parent", "blocked", "blocked")
    b.log("parent", "blocked", {"kind": "needs_input"}, 1)
    b.card("child", "todo", "todo", parents=["parent"])
    b.move("child", "blocked", "blocked", {"kind": "needs_input"})
    b.t += 10
    blocks.reset_failures(b.conn, b.ids["child"], by="human", now=b.t)   # open parent: todo


def kinds_board(tc):
    """A second oracle for the rebuild: cards put in their real final status by hand (or by the
    real `blocks`/`db` calls), each through a kind `dispatch.ready` never emits itself, with the
    expected status written out here. Returns (path, {name: expected status}, ids by name)."""
    path, conn = _board(tc, "k2.db")
    b = Cards(conn)
    _status_cards(b)
    _flow_cards(b)
    b.card("claimed", "ready", "running")               # the one real decision
    conn.execute("UPDATE tasks SET priority=50 WHERE id=?", (b.ids["claimed"],))
    b.t += 10
    rows = dispatch.ready(conn, OFF, {"strong"}, b.t)
    assert rows[0]["id"] == b.ids["claimed"], [r["id"] for r in rows]
    lease.claim(conn, b.ids["claimed"], ttl=900, role="implementer", now=b.t)
    return path, b.want, b.ids


def _cli(*args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        return replay.main(list(args)), out.getvalue()


def _verdict(got):
    return replay.report(got).splitlines()[-2]


class Replay(unittest.TestCase):
    def _at(self, path, claim):
        """The in-memory board as it stood before the claimed event row `claim`."""
        base = replay.snapshot(path)
        self.addCleanup(base.close)
        rebuilt = replay._rebuild(base, {claim["id"]})
        work = sqlite3.connect(":memory:", isolation_level=None)
        work.row_factory = sqlite3.Row
        self.addCleanup(work.close)
        base.backup(work)
        replay._board_at(work, claim["id"], claim["run_id"], claim["created_at"],
                         rebuilt.states[claim["id"]], rebuilt.expiries[claim["id"]],
                         claim["task_id"])
        return base, work

    def test_switches_off_replay_reproduces_the_recorded_order(self):
        path, ids, order = history(self, CFG2)
        self.assertEqual(len(order), 7)           # every card, the child once its parent is done
        self.assertNotEqual(order, [ids[k] for k in "abcdefg"])   # priority set the order
        got = replay.replay(path, dict(CFG2, redesign={}), n=20)
        self.assertEqual(got.recorded_order, order)   # the recorded task_events, read alone
        self.assertEqual(got.replayed_order, order)   # what dispatch.ready picks on the rebuild
        self.assertTrue(got.matched)
        self.assertFalse(any(d.forced for d in got.decisions))
        last3 = replay.replay(path, CFG2, n=3)        # only the last n decisions replay
        self.assertEqual(last3.recorded_order, order[-3:])
        self.assertTrue(last3.matched)

    def test_a_different_rule_shows_as_a_diff(self):
        path, ids, order = history(self, CFG2)
        tight = dict(CFG2, caps=dict(CFG2["caps"], max_running=1))   # the board ran two at a time
        got = replay.replay(path, tight, n=20)
        self.assertFalse(got.matched)
        self.assertIn(None, got.replayed_order)       # a second claim into a full seat is refused

    def test_a_changed_card_is_caught(self):
        path, ids, order = history(self, CFG2)
        db_ = db.connect(path)
        db_.execute("UPDATE tasks SET priority=100 WHERE id=?", (ids["g"],))   # g was claimed last
        db_.close()
        got = replay.replay(path, CFG2, n=20)
        self.assertFalse(got.matched)
        self.assertIn("DIFF", replay.report(got))

    def test_the_board_file_is_never_written(self):
        path, ids, order = history(self, CFG2)
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        replay.replay(path, CFG2, n=20)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir() if p.name != "k.db"),
                         sorted(n for n in ("k.db-wal", "k.db-shm") if (path.parent / n).exists()))

    def test_every_status_moving_kind_rebuilds_to_the_boards_own_status(self):
        path, want, ids = kinds_board(self)
        base = replay.snapshot(path)
        self.addCleanup(base.close)
        rebuilt = replay._rebuild(base, set())
        self.assertEqual({name: rebuilt.status[ids[name]] for name in want}, want)   # table above
        marks = ",".join("?" * len(ids))
        sql = f"SELECT id, status FROM tasks WHERE id IN ({marks})"
        real = dict(base.execute(sql, list(ids.values())))
        self.assertEqual({n: real[i] for n, i in ids.items()}, want)                  # the board
        self.assertEqual(rebuilt.unmapped, [])
        got = replay.replay(path, OFF, n=20)
        self.assertEqual(got.fidelity, (len(want), len(want)))
        self.assertEqual((got.mismatched, got.blind), ([], 1))    # only `timed_out` never moved
        self.assertTrue(got.matched)
        self.assertIn(f"fidelity {len(want)}/{len(want)} cards (1 blind", _verdict(got))

    def test_a_protected_path_refusal_is_a_known_kind_that_moves_nothing(self):
        path, want, ids = kinds_board(self)
        conn = db.connect(path)
        with db.txn(conn):
            db.event(conn, ids["scheduled"], "protected_path_refused",
                     {"path": "contracts/x.md", "task_id": ids["scheduled"], "stage": "review"})
        conn.close()
        base = replay.snapshot(path)
        self.addCleanup(base.close)
        rebuilt = replay._rebuild(base, set())
        self.assertEqual(rebuilt.unmapped, [])
        self.assertEqual(rebuilt.status[ids["scheduled"]], want["scheduled"])

    def test_a_card_the_rebuild_gets_wrong_through_the_window_makes_the_verdict_different(self):
        path, want, ids = kinds_board(self)
        db_ = db.connect(path)
        db_.execute("UPDATE tasks SET status='done' WHERE id=?", (ids["scheduled"],))
        db_.execute("UPDATE task_events SET kind='gave_up_x' WHERE task_id=? AND kind='gave_up'",
                    (ids["gave_up"],))
        db_.close()
        got = replay.replay(path, OFF, n=20)
        self.assertEqual({m.card for m in got.mismatched}, {ids["scheduled"]})
        self.assertEqual(got.unmapped, ["gave_up_x"])
        self.assertEqual(got.recorded_order, got.replayed_order)      # the order itself agrees
        self.assertFalse(got.matched)
        self.assertIn("1 wrong through the window, last kinds: scheduled", _verdict(got))
        self.assertIn("unmapped kinds: gave_up_x", _verdict(got))
        self.assertIn("DIFFERENT", _verdict(got))

    def test_a_wrong_card_that_cannot_affect_dispatch_is_only_reported(self):
        path, want, ids = kinds_board(self)
        db_ = db.connect(path)
        db_.execute("UPDATE tasks SET status='done', branch_name='epic/elsewhere' WHERE id=?",
                    (ids["scheduled"],))
        db_.close()
        got = replay.replay(path, OFF, n=20)
        self.assertEqual([(m.card, m.counts) for m in got.mismatched], [(ids["scheduled"], False)])
        self.assertTrue(got.matched)
        db_ = db.connect(path)                       # the same card running would hold a seat
        db_.execute("UPDATE tasks SET status='running' WHERE id=?", (ids["scheduled"],))
        db_.close()
        self.assertFalse(replay.replay(path, OFF, n=20).matched)

    def test_a_card_finished_by_hand_before_the_window_is_rebuilt_done(self):
        path, want, ids = kinds_board(self)
        got = replay.replay(path, OFF, n=20)
        self.assertTrue(got.matched)                 # `completed_at` gave it a completion point
        db_ = db.connect(path)
        db_.execute("UPDATE tasks SET completed_at=NULL WHERE id=?", (ids["by_hand"],))
        db_.close()
        got = replay.replay(path, OFF, n=20)         # nothing says when it finished: wrong, counted
        self.assertEqual([m.card for m in got.mismatched], [ids["by_hand"]])
        self.assertFalse(got.matched)

    def test_a_forced_decision_counts_against_the_match_and_shows_on_the_verdict(self):
        path, ids, order = history(self, CFG2)
        db_ = db.connect(path)
        first = db_.execute("SELECT task_id FROM task_events WHERE kind='claimed'"
                            " ORDER BY id").fetchone()
        db_.execute("DELETE FROM task_events WHERE task_id=? AND kind='created'",
                    (first["task_id"],))             # the rebuild cannot have the card ready
        db_.close()
        got = replay.replay(path, CFG2, n=20)
        self.assertEqual(got.forced, 1)
        self.assertEqual(got.recorded_order, got.replayed_order)      # only the forcing differs
        self.assertFalse(got.matched)
        self.assertIn("forced 1;", _verdict(got))
        self.assertIn(f"{len(order) - 1}/{len(order)} decisions match", _verdict(got))
        self.assertEqual(_cli("--board", str(path), "--config", CFG_FILE, "-n", "20")[0], 1)

    def test_runs_are_kept_by_time_and_no_run_is_probed_by_pid(self):
        path, ids, order = history(self, CFG2)
        with contextlib.closing(sqlite3.connect(path)) as raw:
            mid = raw.execute("SELECT id, task_id, created_at, run_id FROM task_events"
                              " WHERE kind='claimed' ORDER BY id LIMIT 1 OFFSET 3").fetchone()
            raw.execute("UPDATE task_runs SET worker_pid=1, profile='reviewer' WHERE id>0")
            raw.commit()
        claim = dict(zip(("id", "task_id", "created_at", "run_id"), mid))
        base, work = self._at(path, claim)
        runs = work.execute("SELECT id, started_at, ended_at, worker_pid FROM task_runs").fetchall()
        at = claim["created_at"]
        self.assertTrue(runs)
        self.assertTrue(all(r["started_at"] <= at for r in runs))
        self.assertNotIn(claim["run_id"], [r["id"] for r in runs])    # the decision's own run
        self.assertTrue(all(r["ended_at"] is None or r["ended_at"] <= at for r in runs))
        self.assertTrue(any(r["ended_at"] is not None for r in runs))  # finished ones stay finished
        self.assertTrue(all(r["worker_pid"] is None for r in runs))
        want = {r["task_id"]: json.loads(r["payload"])["expires"] for r in base.execute(
            "SELECT task_id, payload FROM task_events WHERE kind='claimed' AND id<?",
            (claim["id"],))}
        rows = work.execute("SELECT id, claim_expires FROM tasks WHERE status='running'").fetchall()
        self.assertTrue(rows)
        self.assertEqual({r["id"]: r["claim_expires"] for r in rows},
                         {r["id"]: want[r["id"]] for r in rows})   # from the claimed `expires`
        with mock.patch.object(seats.os, "kill", side_effect=AssertionError("os.kill")):
            self.assertTrue(replay.replay(path, CFG2, n=5).matched)

    def test_a_run_that_ended_after_the_decision_is_open_at_it(self):
        path, ids, order = history(self, CFG2)
        base = replay.snapshot(path)
        self.addCleanup(base.close)
        claim = base.execute(
            "SELECT e.id, e.created_at, e.run_id, e.task_id, r.id AS late FROM task_events e"
            " JOIN task_runs r ON e.created_at >= r.started_at AND e.created_at < r.ended_at"
            " AND r.id != e.run_id WHERE e.kind='claimed' ORDER BY e.id").fetchone()
        self.assertIsNotNone(claim)      # a claim made while an earlier card's run still worked
        _, work = self._at(path, claim)
        ended = work.execute("SELECT ended_at FROM task_runs WHERE id=?", (claim["late"],))
        self.assertIsNone(ended.fetchone()[0])

    def test_since_scores_only_decisions_from_that_time(self):
        path, ids, order = history(self, CFG)        # the CLI reads the example config: one epic
        base = replay.snapshot(path)
        self.addCleanup(base.close)
        stamps = [r[0] for r in base.execute(
            "SELECT created_at FROM task_events WHERE kind='claimed' ORDER BY id")]
        later = [o for o, st in zip(order, stamps) if st >= stamps[3]]
        self.assertLess(len(later), len(order))
        got = replay.replay(path, CFG, n=20, since=stamps[3])
        self.assertEqual((got.recorded_order, got.matched), (later, True))
        iso = replay.datetime.fromtimestamp(stamps[3]).isoformat()
        rc, out = _cli("--board", str(path), "--config", CFG_FILE, "--since", iso)
        self.assertEqual(rc, 0)
        self.assertIn(f"replay: {len(later)}/{len(later)} decisions match", out)
        for bad in ("someday", "2099-01-01"):        # not a time; no decisions after it
            self.assertEqual(_cli("--board", str(path), "--config", CFG_FILE, "--since", bad)[0], 2)

    def test_the_verdict_states_its_limits(self):
        path, ids, order = history(self, CFG)
        out = _cli("--board", str(path), "--config", CFG_FILE)[1]
        for words in ("positive decisions only", "today's code", "home=None", "forced 0",
                      "fidelity", "blind"):
            self.assertIn(words, out)

    def test_cli_exit_codes(self):
        path, ids, order = history(self, CFG)
        rc, out = _cli("--board", str(path), "--config", CFG_FILE)
        self.assertEqual(rc, 0)
        self.assertIn(f"replay: {len(order)}/{len(order)} decisions match", out)
        self.assertEqual(_cli("--board", str(path.parent / "nope.db"), "--config", CFG_FILE)[0], 2)


if __name__ == "__main__":
    unittest.main()
