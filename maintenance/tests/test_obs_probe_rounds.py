# mutation-check: targets-predate-range b957739c9 -- rename only, no behaviour pinned ::
#   ReviewRoundsMatchReviewerRound.test_collect_boards_ignores_the_legacy_home
"""t_14e259de (rows 553, 583): `capsule.card.review_rounds` reads `review_scope.reviewer_round`'s
predicate on a board the Capsule tick runs (the `.capsule-owned` marker) and the legacy
`changes_requested` count on a legacy-flow board. maintenance/ never imports capsule, so the
Capsule predicate is a SQL port; the oracle is the real `reviewer_round` run over the same real
capsule board, plus fixed expectations for the shapes row 553 names."""
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["AGENT_OPS_NO_SEND"] = "1"
os.environ.setdefault(
    "EVENTS_SPOOL", str(Path(tempfile.mkdtemp(prefix="events-obsr-")) / "events.jsonl"))
import obs_probe
import board_fence
import review_rounds
from capsule import db, review_scope
from test_obs_probe import NOW
from capsule import legacy_names as _LN  # noqa: E402

INSERT_RUN = ("INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at,"
              " run_id, runner) VALUES (?,?,?,?,10,20,?,'host')")
INSERT_EVENT = "INSERT INTO task_events (task_id, run_id, kind, created_at) VALUES (?,?,?,1)"


def add_run(conn, tid, profile, outcome, n):
    status = outcome or "running"
    run_id = f"r_{tid}_{profile}_{n}"
    return conn.execute(INSERT_RUN, (tid, profile, status, outcome, run_id)).lastrowid


def add_event(conn, tid, kind, run_pk=None):
    conn.execute(INSERT_EVENT, (tid, run_pk, kind))


def make_board(home, name, marked):
    path = home / ".local" / "share" / "capsule" / "boards" / name / "kanban.db"
    db.create_board(path)
    if marked:
        (path.parent / board_fence.OWNED_MARKER).write_text("")
    return db.connect(path)


class ReviewRoundsMatchReviewerRound(unittest.TestCase):
    def setUp(self):
        self.enterContext(unittest.mock.patch.dict(os.environ, {"CAPSULE_HOME": ""}))
        self.home = Path(tempfile.mkdtemp(prefix="obs-rounds-"))
        self.conn = make_board(self.home, "capsule-corp", marked=True)
        cards = [db.new_task(self.conn, title="t", branch="epic/x") for _ in range(7)]
        self.two, self.accepted, self.roles, self.decoy = cards[:4]
        self.both, self.twice, self.wrong_run = cards[4:]
        for n in (1, 2):                              # two completed implementer rounds
            add_run(self.conn, self.two, "implementer", "completed", n)
        held = add_run(self.conn, self.accepted, "implementer", "failed", 1)
        add_event(self.conn, self.accepted, "request_review", held)    # a human accept
        add_run(self.conn, self.accepted, "implementer", "completed", 2)
        add_run(self.conn, self.roles, "implementer", "completed", 1)  # review roles never count
        for role in ("reviewer", "validator", "guide"):
            add_run(self.conn, self.roles, role, "completed", 2)
        add_run(self.conn, self.decoy, "implementer", "failed", 1)     # no round marker at all
        for _ in range(3):
            add_event(self.conn, self.decoy, "changes_requested")
        done = add_run(self.conn, self.both, "implementer", "completed", 1)
        add_event(self.conn, self.both, "request_review", done)        # completed AND named: once
        again = add_run(self.conn, self.twice, "implementer", "failed", 1)
        add_event(self.conn, self.twice, "request_review", again)      # re-accepted: still once
        add_event(self.conn, self.twice, "request_review", again)
        failed = add_run(self.conn, self.wrong_run, "implementer", "failed", 1)
        add_event(self.conn, self.wrong_run, "changes_requested", failed)  # kind, not run, decides

    def got(self):
        return {s.labels["task_id"]: s.value for s in obs_probe.collect_cards(self.home, now=NOW)
                if s.name == "capsule.card.review_rounds"}

    def test_fixed_shapes(self):
        got = self.got()
        shapes = [self.two, self.accepted, self.roles, self.decoy, self.both, self.twice,
                  self.wrong_run]
        self.assertEqual([got[t] for t in shapes], [2, 2, 1, 0, 1, 1, 0])

    def test_every_card_equals_the_real_reviewer_round(self):
        got = self.got()
        for tid in got:
            self.assertEqual(got[tid], review_scope.reviewer_round(self.conn, tid), tid)
        self.assertEqual(len(got), 7)

    def test_collect_boards_ignores_the_legacy_home(self):
        """Row 478 D8: a board under the old kanban home yields nothing."""
        db.create_board(self.home / f".{_LN.NAME}" / "kanban" / "boards" / "legacy" / "kanban.db")
        boards = {s.labels["board"] for s in obs_probe.collect_boards(self.home)}
        self.assertEqual(boards, {"capsule-corp"})

    def test_the_marker_is_the_board_fence_marker(self):
        self.assertEqual(review_rounds.MARKER, board_fence.OWNED_MARKER)

    def test_the_handover_module_is_gone(self):
        import importlib.util
        self.assertIsNone(importlib.util.find_spec("capsule.handover"))


class LegacyFlowBoardKeepsTheLegacyCount(unittest.TestCase):
    def test_an_unmarked_board_counts_changes_requested_events(self):
        self.enterContext(unittest.mock.patch.dict(os.environ, {"CAPSULE_HOME": ""}))
        home = Path(tempfile.mkdtemp(prefix="obs-legacy-"))
        conn = make_board(home, "my-app", marked=False)
        tid = db.new_task(conn, title="t")
        done = add_run(conn, tid, "implementer", "completed", 1)   # would be 1 under row 553
        add_event(conn, tid, "request_review", done)
        for _ in range(2):
            add_event(conn, tid, "changes_requested")
        got = {s.labels["task_id"]: s.value for s in obs_probe.collect_cards(home, now=NOW)
               if s.name == "capsule.card.review_rounds"}
        self.assertEqual(got[tid], 2)
