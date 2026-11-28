"""Row 520 (t_5dfcbd9b, paper gap 1): a rerun or checkpoint restart used to see only its own
summary of earlier rounds, never the earlier runs' own raw files. `prior_runs.paths_for` /
`prior_runs.mount` are the second oracle (docs/ZENKAI.md Z2) - real file paths beside the
summary text.
"""
import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from capsule import contracts, db, lease, paths, prior_runs
from capsule.runners import host_wall
from capsule.tests import tmpclean
from capsule.tests.test_host_wall import DARWIN, Board


class PathsFor(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.board = paths.board_db("test", self.home)
        db.create_board(self.board)
        self.conn = db.connect(self.board)
        self.tid = db.new_task(self.conn, title="x", assignee="dev-frontend", branch="epic/x")
        self.spool_root = paths.board_dir("test", self.home) / "spool"

    def tearDown(self):
        self.conn.close()

    def _run(self, role, run_id, *, with_files=True):
        lease.start_run(self.conn, self.tid, role=role, claim_lock=f"h:{run_id}", expires=1, run_id=run_id, now=1)
        sp = self.spool_root / run_id
        (sp / "report").mkdir(parents=True)
        (sp / "done").mkdir(parents=True)
        if with_files:
            (sp / "report" / "1.json").write_text('{"schema": "report.v1"}')
            (sp / "done" / "1.json").write_text('{"schema": "request.v1"}')
            (sp / "worker.log").write_text("log\n")
        return sp

    def _paths_for(self, exclude="launching", limit=3):
        return prior_runs.paths_for(self.conn, self.spool_root, self.tid, exclude, limit=limit)

    # Z1 intent examples (row 520, executed here as the second oracle beside contracts/intent/implementer.jsonl):

    def test_intent_first_run_has_no_prior_runs(self):
        """Source: row 520. A card's very first dispatch has no earlier task_runs rows at all."""
        self.assertEqual(self._paths_for(), [])

    def test_intent_rerun_after_one_failed_round_sees_that_runs_paths(self):
        """Source: row 520. One prior implementer run -> one entry naming its real files."""
        sp = self._run("implementer", "r1")
        [entry] = self._paths_for()
        self.assertEqual((entry["run_id"], entry["role"]), ("r1", "implementer"))
        self.assertEqual(set(entry["paths"]), {
            str(sp), str(sp / "report" / "1.json"), str(sp / "done" / "1.json"), str(sp / "worker.log")})

    def test_intent_capped_at_three(self):
        """Source: row 520. Five prior runs on one card; only the newest three come back."""
        for i in range(5):
            self._run("implementer", f"r{i}")
        entries = self._paths_for()
        self.assertEqual([e["run_id"] for e in entries], ["r4", "r3", "r2"])

    # -- the cap keeps the newest implementer run --------------------------------------------

    def test_the_newest_implementer_run_survives_three_newer_review_runs(self):
        """Row 520: the most common rerun (one failed review round) must keep the implementer's
        own report even once guide+reviewer+validator all outrank it, at the cost of the oldest
        review run."""
        self._run("implementer", "impl")
        self._run("validator", "val")     # oldest review run - dropped
        self._run("reviewer", "rev")
        self._run("guide", "gd")
        entries = self._paths_for()
        self.assertEqual([e["run_id"] for e in entries], ["gd", "rev", "impl"])

    def test_with_no_implementer_run_the_cap_is_just_the_newest_three(self):
        for role, rid in [("reviewer", "r1"), ("validator", "r2"), ("guide", "r3")]:
            self._run(role, rid)
        entries = self._paths_for()
        self.assertEqual([e["run_id"] for e in entries], ["r3", "r2", "r1"])

    # -- mechanics -------------------------------------------------------------------------

    def test_only_existing_files_are_named(self):
        self._run("implementer", "r1", with_files=False)
        [entry] = self._paths_for()
        self.assertEqual(len(entry["paths"]), 1)   # the bare spool dir only

    def test_review_roles_count_too(self):
        self._run("reviewer", "r1")
        self._run("guide", "r2")
        self._run("validator", "r3")
        entries = self._paths_for()
        self.assertEqual({e["role"] for e in entries}, {"reviewer", "guide", "validator"})

    def test_a_non_review_role_is_never_named(self):
        self._run("korin", "r1")
        self.assertEqual(self._paths_for(), [])

    def test_paths_for_never_touches_the_live_spool_files_mode(self):
        """Row 520: `paths_for` is read-only discovery, nothing more - the copy-and-chmod
        happens in `mount`."""
        sp = self._run("implementer", "r1")
        before = (sp / "report" / "1.json").stat().st_mode
        self._paths_for()
        self.assertEqual((sp / "report" / "1.json").stat().st_mode, before)
        self.assertTrue(before & stat.S_IWUSR)   # still writable - untouched, not "still 0444"

    def test_the_launching_run_is_excluded_by_id_not_by_a_missing_spool_dir(self):
        """Row 520: `_launch` claims the new run's task_runs row before calling `paths_for`;
        passing its real run_id and filtering in SQL (`run_id != ?`) means this holds even if
        something had already created a spool dir for it."""
        for i in range(3):
            self._run("implementer", f"r{i}")
        self._run("implementer", "launching")   # give it a spool dir too, unlike a real launch
        entries = prior_runs.paths_for(self.conn, self.spool_root, self.tid, "launching")
        self.assertEqual([e["run_id"] for e in entries], ["r2", "r1", "r0"])


class BlindValidator(Board):
    """A validator run whose job carries blind rows is never copied into the implementer's `prior/`
    (its report and log quote the blind text); the real wall is the second oracle (D161)."""

    def setUp(self):
        super().setUp()
        self.addCleanup(tmpclean.sweep, self.root)             # the mirror is read-only
        db.create_board(self.board / "kanban.db")
        self.conn = db.connect(self.board / "kanban.db")
        self.addCleanup(self.conn.close)
        self.tid = db.new_task(self.conn, title="x", assignee="dev-frontend", branch="epic/x")

    def run_of(self, role, run_id, cmd, text):
        lease.start_run(self.conn, self.tid, role=role, claim_lock=f"h:{run_id}", expires=1,
                        run_id=run_id, now=1)
        sp = self.spools / run_id
        (sp / "report").mkdir(parents=True)
        (sp / "report" / "1.json").write_text(json.dumps({"excerpt": text}))
        (sp / "worker.log").write_text(f"log {text}\n")
        (sp / "job.json").write_text(json.dumps({"workspace": str(self.ws), "inputs": {
            "outputs": [{"cmd": cmd, "given": text, "expect": "E", "observed": "O"}]}}))
        return sp

    @DARWIN
    def test_a_validator_with_blind_rows_is_not_copied_and_one_without_is(self):
        self.run_of("implementer", "imp1", "make test", "IMPL_NOTE")
        carrying = self.run_of("validator", "val1", "blind:t1.test_x", "PLANTED_given_7_expect_8")
        self.run_of("validator", "val2", "make test", "PLAIN_OK")
        entries = prior_runs.paths_for(self.conn, self.spools, self.tid, "run1")
        self.assertEqual([e["run_id"] for e in entries], ["val2", "imp1"])
        prior_runs.mount({"prior_runs": entries}, self.spool, {})
        argv = host_wall.apply(["claude"], self.plan())[:3]
        for text, found in (("PLANTED", False), ("PLAIN_OK", True)):
            grep = subprocess.run(argv + ["/usr/bin/grep", "-rl", text, str(self.spool / "prior")],
                                  capture_output=True, text=True)
            self.assertEqual((grep.returncode == 0, "val2" in grep.stdout), (found, found), text)
        refused = subprocess.run(argv + ["/bin/cat", str(carrying / "report" / "1.json")],
                                 capture_output=True, text=True)
        self.assertEqual((refused.stdout, refused.returncode != 0), ("", True))   # the original too

    def test_a_validator_with_blind_rows_does_not_use_up_a_slot_of_the_three(self):
        for k in range(4):
            self.run_of("validator", f"blind{k}", "blind:t1.test_x", "PLANTED")
        for k in range(3):
            self.run_of("reviewer", f"rev{k}", "make test", "ok")
        entries = prior_runs.paths_for(self.conn, self.spools, self.tid, "run1")
        self.assertEqual([e["run_id"] for e in entries], ["rev2", "rev1", "rev0"])


class Mount(unittest.TestCase):
    """Row 520: mirrors prior runs' files as copies, not a grant onto the live spool."""

    def setUp(self):
        self.dest_root = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.dest_root)
        self.sp = self.dest_root / "launching-run"
        self.sp.mkdir()
        self.src = self.dest_root / "r1"
        (self.src / "report").mkdir(parents=True)
        (self.src / "report" / "1.json").write_text('{"schema": "report.v1"}')
        (self.src / "worker.log").write_text("log\n")
        self.prior = [{"run_id": "r1", "role": "implementer", "started_at": 1,
                      "paths": [str(self.src), str(self.src / "report" / "1.json"), str(self.src / "worker.log")]}]

    def test_a_write_under_the_mirrored_dir_is_refused(self):
        inputs, limits = prior_runs.mount({"prior_runs": self.prior}, self.sp, {})
        copy = Path([p for p in inputs["prior_runs"][0]["paths"] if p.endswith("1.json")][0])
        self.assertTrue(copy.is_file())
        with self.assertRaises(PermissionError):
            copy.write_text("mutated")
        with self.assertRaises(PermissionError):
            (copy.parent / "new.json").write_text("planted")
        self.assertEqual(limits["add_dirs"], [str(self.sp / "prior")])

    def test_the_original_prior_run_files_keep_their_mode(self):
        before = (self.src / "report" / "1.json").stat().st_mode
        prior_runs.mount({"prior_runs": self.prior}, self.sp, {})
        self.assertEqual((self.src / "report" / "1.json").stat().st_mode, before)
        (self.src / "report" / "1.json").write_text("still writable")   # never raises

    def test_no_prior_runs_is_a_no_op(self):
        inputs, limits = prior_runs.mount({"prior_runs": []}, self.sp, {"tools": "x"})
        self.assertEqual(inputs, {"prior_runs": []})
        self.assertEqual(limits, {"tools": "x"})
        self.assertFalse((self.sp / "prior").exists())


class ContractSentence(unittest.TestCase):
    """Row 520: pins the rendered implementer contract's `gets:` sentence naming prior runs."""

    def test_the_rendered_implementer_contract_names_prior_runs(self):
        rendered = contracts.render("implementer")
        self.assertIn("on a rerun, the last runs' own spool paths", rendered)
        self.assertIn("read these before deciding what is already done", rendered)
