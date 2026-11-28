"""Shared fixtures of the land-time blind tests (`test_lander_blind*.py`): a card on the epic
branch with its blind tests landed in a real blind repo, `gate` mode, real git over the World
fixture (a bare remote and a clone), and the strong runner's stand-in, which runs the run line on
the host inside the lander's own clone with `/blind` and `/out` mapped to the real folders."""
import json
import os
import subprocess
import time
from pathlib import Path

from capsule import db, incidents, interventions, lander, lander_blind, paths
from capsule.tests.test_blind_run import PASSING, TESTS, blind_repo, record, sh
from capsule.tests.test_lander import BRANCH, World

BOARD = "sample-app"
ACCEPTANCE = "Acceptance:\n- the page greets\n"
IDENT = ("-c", "user.name=t", "-c", "user.email=t@t")
HOURS_72 = 72 * 3600
__all__ = ["Strong", "Landing", "BOARD", "ACCEPTANCE", "IDENT", "HOURS_72", "PASSING", "TESTS"]


class Strong:
    """The strong runner's stand-in: records `start` and each exec's timeout, runs the run line on
    the host in the clone it was handed (read-only in real life). `late`: the run finishes and
    writes its results, then the exec times out. `gone`: the container vanishes mid-run while docker
    still answers (a restarted container); `dies`: the daemon goes down with it. `gone_early`: it
    is gone before the first exec."""

    def __init__(self, up=True, after=None, tamper=None, boom=None, start_error=None,
                 stop_error=None, dies=False, late=False, gone=False, gone_early=False):
        self.up, self.after, self.tamper, self.boom = up, after, tamper, boom
        self.start_error, self.stop_error, self.dies = start_error, stop_error, dies
        self.dead = int(gone_early)
        self.late, self.gone = late, gone
        self.started, self.stopped, self.snaps, self.heads, self.timeouts = [], [], [], [], []

    def available(self):
        return self.up

    def alive(self, name):
        return not self.dead

    def start(self, run_id, ws, **kw):
        if self.start_error:
            raise self.start_error
        self.started.append((run_id, str(ws), kw))
        self.ws = ws
        self.mounts = dict(m.split(":")[:2][::-1] for m in kw.get("mounts", ()))   # dst -> src
        self.snaps.append(Path(self.mounts["/blind"]))
        return "capsule-" + run_id[:12]

    def exec(self, name, cmd, timeout):
        self.timeouts.append(timeout)
        if self.boom:
            raise self.boom
        if self.dies or self.gone:         # the container is gone; `dies`: the daemon with it
            self.up, self.dead = self.up and not self.dies, 1
            return 1, ""
        self.heads.append(sh("git", "-C", str(self.ws), "rev-parse", "HEAD").strip())
        line = cmd.replace("/blind", self.mounts["/blind"]).replace(
            "--card", f"--out {self.mounts['/out']} --card", 1)
        done = subprocess.run(["sh", "-c", line], cwd=self.ws, capture_output=True, text=True,
                              env={**os.environ, "BLIND_WORKSPACE": str(self.ws)})
        if self.after:
            self.after(Path(self.ws))
        if self.late:
            raise subprocess.TimeoutExpired("run", timeout)
        text = done.stdout + done.stderr
        return done.returncode, self.tamper(text) if self.tamper else text

    def stop(self, name):
        self.stopped.append(name)
        if self.stop_error:
            raise self.stop_error


class Landing(World):
    """A card on the epic branch, its blind tests landed in a real blind repo, `gate` mode."""
    mode = "gate"
    source = TESTS         # one test passes, one fails
    body = ACCEPTANCE

    def setUp(self):
        super().setUp()
        self.home = self.dir / "home"
        board = paths.board_db(BOARD, self.home)
        board.parent.mkdir(parents=True, exist_ok=True)
        db.create_board(board)
        self.conn = db.connect(board)
        self.addCleanup(self.conn.close)
        self.card = db.new_task(self.conn, title="card", body=self.body, assignee="dev",
                                branch=BRANCH, status="done")
        self.fix = db.new_task(self.conn, title="fix", status="ready")
        self.put("hello.txt", "hi\n", "card work")
        self.blind = blind_repo(self.dir, self.card, self.source)
        record(self.conn, self.card, self.blind)
        self.cfg.update(board=BOARD, epics=[BRANCH], redesign={"blind_tests": self.mode},
                        blind_tests={"repo": str(self.blind), "adapter": "unittest"})

    def land(self, strong, **kw):
        return self.check(task_id=self.card, conn=self.conn, runner=strong, **kw)

    def landed(self, strong, **kw):
        """One landing attempt as the lander records it: the verdict, then its `landing_checked`
        event and the card's move."""
        result = self.land(strong, **kw)
        self.conn.execute("UPDATE tasks SET status='done', block_kind=NULL WHERE id=?",
                          (self.card,))             # as it stands when a landing is attempted
        lander.record(self.conn, self.card, result, limit=3, board=BOARD)
        return result

    def checks(self, result):
        return {c["name"]: c for c in result["checks"]}

    def events(self, kind):
        return [json.loads(r[0]) for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=?", (self.card, kind))]

    def dump(self):
        """Everything the landing wrote about the card, as one text."""
        rows = self.conn.execute("SELECT payload FROM task_events WHERE task_id=?", (self.card,))
        sent = self.conn.execute("SELECT * FROM outbox").fetchall()
        saw = [tuple(r) for r in self.conn.execute("SELECT evidence, role FROM incidents")]
        return json.dumps([r[0] for r in rows]) + json.dumps([tuple(r) for r in sent]) + str(saw)

    def commit_tests(self, source):
        (self.blind / "cards" / self.card / "test_it.py").write_text(source)
        sh("git", "-C", str(self.blind), "add", "-A")
        sh("git", "-C", str(self.blind), *IDENT, "commit", "-q", "-m", "tests")

    def task(self):
        return self.conn.execute("SELECT * FROM tasks WHERE id=?", (self.card,)).fetchone()

    def held_for(self, reason, ago):
        """The validator's hold of the card for `reason`, `ago` seconds ago."""
        db.event(self.conn, self.card, "blocked", {
            "reason": reason, "kind": "needs_input", "recurrences": 1,
            "cause": lander_blind.cause(self.task(), reason)}, now=int(time.time()) - ago)

    def accepted(self, ago, author="owner"):
        """A person's `accept` of that hold, `ago` seconds ago, applied, naming the fix card."""
        db.event(self.conn, self.card, "intervention", {
            "kind": "accept", "text": f"the tests will not come: fix card {self.fix}",
            "author": author}, now=int(time.time()) - ago)
        last = self.conn.execute("SELECT MAX(id) FROM task_events").fetchone()[0]
        interventions.stamp_outcome(self.conn, self.card, last, True, "accept_refused")

    def incident_roles(self):
        return [i["role"] for i in incidents.listing(self.conn)]
