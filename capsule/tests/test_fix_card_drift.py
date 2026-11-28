"""inc_43e0584d: releasing a Korin fix card that main overtook lists the main commits."""
import json
import subprocess
from unittest import mock

from capsule import blocks, bundles, db, fix_card_drift, incidents, interventions, runaway
from capsule.tests.test_tick import BRANCH, TickCase, git

BODY = "## Incident inc_x\n\nChange capsule/guide_notes.py and add the test.\n"
H = fix_card_drift.HEADING


class MainMoved(TickCase):
    def setUp(self):
        super().setUp()
        self.repo = self.home / "main_repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        for name in ("guide_notes.py", "other.py"):
            self.commit(name, "v0", "seed " + name)
        self.sha = self.head()
        self.patch = mock.patch.object(fix_card_drift.paths, "REPO", self.repo)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def commit(self, name, text, subject):
        (self.repo / "capsule").mkdir(exist_ok=True)
        (self.repo / "capsule" / name).write_text(text)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", subject)

    def head(self):
        return subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()

    def hold(self, tid, kind="needs_input"):
        self.conn.execute("UPDATE tasks SET status='blocked', block_kind=? WHERE id=?", (kind, tid))

    def status(self, tid):
        return self.conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0]

    def card(self, body=BODY, main_sha="head", incident=True, kind="needs_input"):
        tid = db.new_task(self.conn, title="fix", body=body, assignee="dev-backend",
                          branch=BRANCH, now=5)
        self.hold(tid, kind)
        if incident:
            sha = self.sha if main_sha == "head" else main_sha
            inc, _ = incidents.open_incident(self.conn, board="test", kind="crash", key="k" + tid,
                                             now=6, main_sha=sha)
            self.conn.execute("UPDATE incidents SET fix_card=? WHERE id=?", (tid, inc))
        return tid

    def parked(self):
        tid = self.card()
        self.conn.execute("UPDATE tasks SET status='ready', block_kind=NULL WHERE id=?", (tid,))
        blocks.park(self.conn, tid, "wait", now=7)
        return tid

    def release(self, tid, kind):
        doc = {"kind": kind, "text": "go"}
        interventions.apply(self.conn, self.cfg, tid, doc, "r.json", "r.json", 10)

    def body(self, tid):
        return self.conn.execute("SELECT body FROM tasks WHERE id=?", (tid,)).fetchone()[0]

    def amended(self, tid):
        return [json.loads(r[0]) for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='contract_amended'", (tid,))]

    def test_reset_of_a_parked_korin_fix_card_lists_the_main_commits_on_its_files(self):
        tid = self.parked()
        self.commit("guide_notes.py", "v1", "drop the hunk")
        short = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "--short", "HEAD"],
                               capture_output=True, text=True).stdout.strip()
        self.release(tid, "reset")
        body = self.body(tid)
        self.assertTrue(body.startswith(BODY))
        self.assertIn(H, body)
        self.assertIn(f"{short} drop the hunk", body)
        events = self.amended(tid)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["issuer"], "harness")
        self.assertEqual(events[0]["body_hash_before"], bundles.card_hash(BODY))
        self.assertEqual(events[0]["body_hash_after"], bundles.card_hash(body))

    def test_unblock_of_a_fix_card_hold_does_the_same(self):
        tid = self.card()
        self.commit("guide_notes.py", "v1", "drop the hunk")
        self.release(tid, "unblock")
        self.assertIn("drop the hunk", self.body(tid))
        self.assertEqual(len(self.amended(tid)), 1)

    def test_the_drift_amendment_keeps_the_spend_before_the_unblock(self):
        tid = self.card()
        for i in range(24):   # the floor of 8 runs makes the limit 24
            self.conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at,"
                              " outcome, run_id) VALUES (?,?,?,?,?,?)",
                              (tid, "implementer", "failed", 1, "crashed", f"r{i}"))
        self.assertEqual((runaway.check(self.conn, self.cfg, tid) or ("none",))[0], "runs")
        self.commit("guide_notes.py", "v1", "drop the hunk")
        self.release(tid, "unblock")
        self.assertIn("drop the hunk", self.body(tid))
        self.assertIn(H, self.body(tid))
        self.assertIn("A human answer written at this release stays in force.", self.body(tid))
        self.assertEqual([e["issuer"] for e in self.amended(tid)], ["harness"])
        self.assertEqual((runaway.check(self.conn, self.cfg, tid) or ("none",))[0], "runs")

    def test_commit_on_an_unnamed_file_is_not_listed(self):
        tid = self.card()
        self.commit("other.py", "v1", "other change")
        self.release(tid, "unblock")
        self.assertEqual(self.body(tid), BODY)
        self.assertEqual(self.amended(tid), [])

    def unchanged_after_release(self, body):
        tid = self.card(body=body)
        self.commit("guide_notes.py", "v1", "drop the hunk")
        self.release(tid, "unblock")
        self.assertEqual(self.body(tid), body)
        self.assertEqual(self.amended(tid), [])

    def test_path_named_only_under_prior_verdicts_is_not_used(self):
        self.unchanged_after_release(
            "## Incident\n\nChange the thing.\n\n## Prior verdicts\n\n"
            "- see capsule/guide_notes.py\n")

    def test_path_named_only_in_a_quoted_human_file_is_not_used(self):
        self.unchanged_after_release(
            "## Incident\n\nChange the thing.\n\n## Human files named by this card\n\n"
            "### 5.json\n- task_id: t_x\n\nsee capsule/guide_notes.py\n\n## Prior verdicts\n\n"
            "- row 1\n")

    def test_path_in_an_earlier_section_is_not_used(self):
        intro = fix_card_drift.INTRO
        old = f"{H}\n\n{intro}\n- abc1234 fix capsule/guide_notes.py\n{fix_card_drift.CLOSE}\n"
        tid = self.card(body=f"Change the thing.\n\n{old}")
        self.commit("guide_notes.py", "v1", "drop the hunk")
        self.release(tid, "unblock")
        self.assertEqual(self.body(tid), "Change the thing.\n")
        self.assertEqual(len(self.amended(tid)), 1)

    def test_body_naming_no_path_gets_no_section(self):
        self.unchanged_after_release("## Incident\n\nChange the thing.\n")

    def test_two_incidents_use_the_oldest_main_sha(self):
        tid = self.card()
        self.commit("guide_notes.py", "v1", "landed between")
        later, _ = incidents.open_incident(
            self.conn, board="test", kind="crash", key="k2", now=8, main_sha=self.head())
        self.conn.execute("UPDATE incidents SET fix_card=? WHERE id=?", (tid, later))
        self.release(tid, "unblock")
        self.assertIn("landed between", self.body(tid))

    def test_card_of_no_incident_is_unchanged(self):
        self.commit("guide_notes.py", "v1", "drop the hunk")
        for kind in ("unblock", "reset"):
            tid = self.card(incident=False)
            self.release(tid, kind)
            self.assertEqual(self.body(tid), BODY)

    def test_incident_without_main_sha_leaves_body_and_releases(self):
        tid = self.card(main_sha=None)
        self.commit("guide_notes.py", "v1", "drop the hunk")
        self.release(tid, "unblock")
        self.assertEqual(self.body(tid), BODY)
        self.assertEqual(self.status(tid), "ready")

    def test_git_failure_leaves_body_and_releases(self):
        tid = self.card()
        self.commit("guide_notes.py", "v1", "drop the hunk")
        for err in (subprocess.TimeoutExpired("git", 1), OSError("no git")):
            with mock.patch.object(fix_card_drift.subprocess, "run", side_effect=err):
                self.release(tid, "unblock")
            self.assertEqual(self.body(tid), BODY)
            self.assertEqual(self.status(tid), "ready")
            self.hold(tid)

    def test_git_failure_on_second_release_keeps_the_section(self):
        tid = self.card()
        self.commit("guide_notes.py", "v1", "first")
        self.release(tid, "unblock")
        before = self.body(tid)
        self.hold(tid)
        with mock.patch.object(fix_card_drift.subprocess, "run", side_effect=OSError("no git")):
            self.release(tid, "unblock")
        self.assertEqual(self.body(tid), before)
        self.assertEqual(len(self.amended(tid)), 1)

    def test_bad_sha_is_a_git_failure(self):
        tid = self.card(main_sha="deadbeef" * 5)
        self.commit("guide_notes.py", "v1", "drop the hunk")
        self.release(tid, "unblock")
        self.assertEqual(self.body(tid), BODY)
        self.assertEqual(self.status(tid), "ready")

    def test_body_quoting_the_heading_inline_keeps_its_text(self):
        body = (f"Change capsule/guide_notes.py. Write `{H}` and then\nmore text.\n\n"
                "## Next\n\nkeep capsule/x.py\n")
        tid = self.card(body=body)
        self.commit("guide_notes.py", "v1", "drop the hunk")
        self.release(tid, "unblock")
        new = self.body(tid)
        self.assertTrue(new.startswith(body))
        self.assertIn("drop the hunk", new)

    def test_release_twice_without_a_new_commit_records_one_event(self):
        tid = self.card()
        self.commit("guide_notes.py", "v1", "drop the hunk")
        self.release(tid, "unblock")
        first = self.body(tid)
        self.hold(tid)
        self.release(tid, "unblock")
        self.assertEqual(self.body(tid), first)
        self.assertEqual(len(self.amended(tid)), 1)

    def test_reset_of_a_running_card_changes_no_body(self):
        tid = self.card()
        self.commit("guide_notes.py", "v1", "drop the hunk")
        self.conn.execute("UPDATE tasks SET status='running', block_kind=NULL WHERE id=?", (tid,))
        self.release(tid, "reset")
        self.assertEqual(self.body(tid), BODY)
        self.assertEqual(self.amended(tid), [])

    def test_refused_reset_changes_no_body(self):
        tid = self.card()
        self.conn.execute("UPDATE tasks SET branch_name='nowhere' WHERE id=?", (tid,))
        self.commit("guide_notes.py", "v1", "drop the hunk")
        self.release(tid, "reset")
        self.assertEqual(self.status(tid), "blocked")
        self.assertEqual(self.body(tid), BODY)
        self.assertEqual(self.amended(tid), [])

    def test_refused_release_changes_no_body(self):
        self.commit("guide_notes.py", "v1", "drop the hunk")
        parked = self.parked()
        self.release(parked, "unblock")           # a parked card refuses unblock
        self.assertEqual(self.body(parked), BODY)
        stale = self.card(kind="spec_stale")
        self.release(stale, "unblock")
        self.assertEqual(self.body(stale), BODY)
        self.assertEqual(self.amended(parked) + self.amended(stale), [])

    def test_amending_the_only_path_out_removes_the_section(self):
        tid = self.card()
        self.commit("guide_notes.py", "v1", "first")
        self.release(tid, "unblock")
        self.conn.execute("UPDATE tasks SET body=? WHERE id=?",
                          (self.body(tid).replace("capsule/guide_notes.py", "the notes"), tid))
        self.hold(tid)
        self.release(tid, "unblock")
        body = self.body(tid)
        self.assertNotIn(H, body)
        self.assertNotIn("first", body)
        self.assertEqual(len(self.amended(tid)), 2)

    def test_note_appended_after_the_section_survives_the_next_release(self):
        tid = self.card()
        self.commit("guide_notes.py", "v1", "first")
        self.release(tid, "unblock")
        note = "Amend note 4: fix the marker.\nKeeps: none\n"
        self.conn.execute("UPDATE tasks SET body=body || ? WHERE id=?", ("\n" + note, tid))
        self.commit("guide_notes.py", "v2", "second")
        self.hold(tid)
        self.release(tid, "unblock")
        body = self.body(tid)
        self.assertIn(note, body)
        self.assertEqual(body.count(H), 1)
        self.assertIn("second", body)

    def test_dash_note_appended_after_the_section_survives_the_next_release(self):
        tid = self.card()
        self.commit("guide_notes.py", "v1", "first")
        self.release(tid, "unblock")
        note = "- Amend note 4: fix the marker.\nKeeps: none\n"
        self.conn.execute("UPDATE tasks SET body=body || ? WHERE id=?", ("\n" + note, tid))
        self.commit("guide_notes.py", "v2", "second")
        self.hold(tid)
        self.release(tid, "unblock")
        body = self.body(tid)
        self.assertIn(note, body)
        self.assertEqual(body.count(H), 1)
        self.assertIn("second", body)

    def test_commit_shaped_note_after_the_section_survives_the_next_release(self):
        tid = self.card()
        self.commit("guide_notes.py", "v1", "first")
        self.release(tid, "unblock")
        note = "- 87000558 landed the hunk\nKeeps: none\n"
        self.conn.execute("UPDATE tasks SET body=body || ? WHERE id=?", ("\n" + note, tid))
        self.commit("guide_notes.py", "v2", "second")
        self.hold(tid)
        self.release(tid, "unblock")
        body = self.body(tid)
        self.assertIn(note, body)
        self.assertEqual(body.count(H), 1)
        self.assertIn("second", body)

    def test_second_release_replaces_the_section(self):
        tid = self.card()
        self.commit("guide_notes.py", "v1", "first")
        self.release(tid, "unblock")
        self.commit("guide_notes.py", "v2", "second")
        self.hold(tid)
        self.release(tid, "unblock")
        body = self.body(tid)
        self.assertEqual(body.count(H), 1)
        self.assertIn("first", body)
        self.assertIn("second", body)
        self.assertTrue(body.startswith(BODY))

    def test_subject_is_cleaned_and_lines_are_capped(self):
        tid = self.card()
        for i in range(fix_card_drift.MAX_LINES + 3):
            self.commit("guide_notes.py", f"x{i}", f"n{i}")
        self.commit("guide_notes.py", "v1", "a|b `c`")
        self.release(tid, "unblock")
        body = self.body(tid)
        section = body[body.index(H):]
        self.assertIn("a b c", section)
        self.assertNotIn("`", section)
        self.assertNotIn("|", section)
        self.assertLessEqual(section.count("\n- "), fix_card_drift.MAX_LINES + 1)
        self.assertIn("more commits not listed", section)
