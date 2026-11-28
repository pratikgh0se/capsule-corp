# mutation-check: targets-predate-range b957739c9 -- owner-word rename, no behaviour pinned ::
#   CardPrecheck.test_a_missing_file_sends_the_card_to_the_guide_and_names_the_path,
#   CardPrecheck.test_ask_owner_leaves_the_card_waiting_as_a_needs_you_item,
#   CardPrecheck.test_the_guide_gets_the_card_text_and_the_missing_list
"""D84 / D44: the card pre-flight claim check (`capsule/claims.py`), no model call. A card's
backticked repo paths are checked against the clone's HEAD, read from git, before its first
dispatch; a mismatch sends the card to the guide (the runaway route). A card whose `File set:`
block names a protected path parks (row 395) on every dispatch. Anything else changes nothing."""
import contextlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from capsule import blocks, claims, intent, runaway
from capsule.tests.test_tick import GUIDE_OK, IMPL_DONE, TickCase, git

TIMER_PY = "def start_timer():\n    pass\n"


def decision(choice, context="`app/stale_note.py` is now `app/timer.py`; edit that one"):
    return {"schema": "decision.v1", "question": "claims check", "recommended": choice,
            "context": context, "options": [{"id": i, "label": i} for i in runaway.IDS]}


class ClaimsMismatch(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        (self.repo / "app").mkdir()
        (self.repo / "app" / "timer.py").write_text(TIMER_PY)
        os.symlink("/dev/zero", self.repo / "app" / "zero.bin")   # a committed link to a device
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "init")
        (self.repo / "app" / "untracked.py").write_text("x = 1\n")

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def mismatch(self, text):
        return claims._claims_mismatch(text, self.repo)

    def test_claims_that_all_exist_give_no_mismatch(self):
        card = "Edit `app/timer.py:3` and `app/`, see `app/timer.py#L2`."
        self.assertEqual(self.mismatch(card), [])

    def test_a_missing_path_is_named(self):
        got = self.mismatch("Fix `app/gone.py` and `app/timer.py`.")
        self.assertEqual(len(got), 1)
        self.assertIn("app/gone.py", got[0])

    def test_function_names_are_not_claims(self):
        self.assertEqual(self.mismatch("Call `stop_timer()` and `class Ghost` and `def nope`."), [])

    def test_prose_and_unparseable_text_make_no_claims(self):
        texts = ("", "no backticks at all, app/gone.py in prose", "an `unclosed tick app/gone.py",
                 "`a b/c.py` `../../etc/passwd.txt` `<dir>/x.py` `https://x.io/a.py` `read/write`")
        for text in texts:
            self.assertEqual(self.mismatch(text), [], text)

    def test_a_path_the_card_creates_is_exempt_on_every_line(self):
        card = ("- Files: create `app/screens/pet.py`\n"
                "- [ ] Tests first, `app/screens/pet.py`\n"
                "Rewrite `app/old.py`, add `app/more.py` and build `app/b.py`.\n"
                "Then wire `app/screens/pet.py` and `app/b.py` into `app/timer.py`.")
        self.assertEqual(self.mismatch(card), [])

    def test_a_folder_the_card_puts_a_new_file_in_and_tests_first_files_are_exempt(self):
        card = ("- Files: create `app/fixtures/new/golden.md`; touch `app/fixtures/new/`\n"
                "- [ ] Tests first, `app/ui/test_pet.py`")
        self.assertEqual(self.mismatch(card), [])

    def test_only_paths_under_a_folder_the_repo_has_count(self):
        card = "Notes in `scratchpad/plan.md`, `Boards/sample-app.md` and `other-repo/x.py`."
        self.assertEqual(self.mismatch(card), [])
        self.assertEqual(len(self.mismatch(card + " Also `app/gone.py`.")), 1)

    def test_a_committed_link_to_a_device_is_read_as_an_object_not_followed(self):
        started = time.monotonic()
        self.assertEqual(self.mismatch("Uses `app/zero.bin`."), [])
        self.assertLess(time.monotonic() - started, 5)

    def test_only_committed_content_counts(self):
        got = self.mismatch("Edit `app/untracked.py`.")
        self.assertEqual(len(got), 1)
        self.assertIn("app/untracked.py", got[0])

    def test_no_claims_never_runs_git_but_claims_need_a_readable_repo(self):
        self.assertEqual(claims._claims_mismatch("nothing here", self.repo / "missing"), [])
        with self.assertRaises(Exception):
            claims._claims_mismatch("Edit `app/timer.py`.", self.repo / "missing")


class CardCase(TickCase):
    def setUp(self):
        super().setUp()
        self.cfg["korin"] = dict(self.cfg["korin"], enabled=False)
        self.write_cfg()

    def commit_to_origin(self, rel, text):
        origin = self.home / "origin"
        (origin / rel).parent.mkdir(parents=True, exist_ok=True)
        (origin / rel).write_text(text)
        git(origin, "add", ".")
        git(origin, "commit", "-qm", f"add {rel}")

    def set_body(self, body):
        self.conn.execute("UPDATE tasks SET body=? WHERE id=?", (body, self.tid))
        self.conn.commit()

    def events(self, kind):
        return [json.loads(r[0] or "{}") for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
            (self.tid, kind))]

    def ruling_runs(self):
        return [r for r in self.runs("guide") if json.loads(r["metadata"] or "{}").get("ruling_of")]

    def outbox(self):
        sql = "SELECT body FROM outbox WHERE thread_key=? ORDER BY id"
        rows = self.conn.execute(sql, (self.tid,))
        return [r[0] for r in rows]

    def run_ticks(self, n=8):
        for _ in range(n):
            self.tick.once()

    def stale_card(self):
        self.commit_to_origin("app/timer.py", TIMER_PY)
        self.set_body("Fix the bug in `app/stale_note.py`.")
        self.script["implementer"] = [[IMPL_DONE]]


class CardPrecheck(CardCase):
    def test_claims_that_all_exist_leave_dispatch_unchanged(self):
        self.commit_to_origin("app/timer.py", TIMER_PY)
        self.set_body("Wire `app/timer.py` and `app/`.")
        self.script["implementer"] = [[IMPL_DONE]]
        self.assertEqual(self.tick.once()["dispatched"], 1)
        self.assertEqual(len(self.runs("implementer")), 1)
        self.assertEqual(self.events("claims"), [])

    def test_a_missing_file_sends_the_card_to_the_guide_and_names_the_path(self):
        self.stale_card()
        self.script["guide"] = [[decision("ask-owner", "only the owner can say")]]
        self.assertEqual(self.tick.once()["dispatched"], 0)
        task = self.task()
        self.assertEqual((task["status"], task["block_kind"]), ("triage", "claims"))
        self.assertEqual(self.runs("implementer"), [])
        (ev,) = self.events("claims")
        self.assertTrue(ev["pass"])
        self.assertEqual(len(ev["missing"]), 1)
        self.assertIn("app/stale_note.py", ev["missing"][0])
        self.assertTrue(any("app/stale_note.py" in line for line in self.outbox()))

    def test_the_guide_gets_the_card_text_and_the_missing_list(self):
        self.stale_card()
        self.script["guide"] = [[decision("ask-owner", "only the owner can say")]]
        self.run_ticks(3)
        (run,) = self.ruling_runs()
        sp = self.spool_dir(run["run_id"])
        packet = json.loads((sp / "job.json").read_text())["inputs"]["ruling"]
        self.assertEqual(packet["kind"], "claims")
        self.assertIn("Fix the bug in `app/stale_note.py`.", packet["card"])
        self.assertIn("app/stale_note.py", packet["missing"][0])
        prompt = json.loads((sp / "bundle.json").read_text())["prompt"]
        for part in ("claims check", "app/stale_note.py", "continue", "ask-owner"):
            self.assertIn(part, prompt)
        self.assertNotIn("sibling instance", prompt)

    def test_continue_releases_the_card_with_the_correction_and_it_is_not_held_twice(self):
        self.stale_card()
        fix = "`app/stale_note.py` is now `app/timer.py`; edit that one"
        self.script["guide"] = [[decision("continue", fix)], [GUIDE_OK]]
        self.run_ticks(6)
        runs = self.runs("implementer")
        self.assertEqual(len(runs), 1)
        job = json.loads((self.spool_dir(runs[0]["run_id"]) / "job.json").read_text())
        self.assertEqual(job["inputs"]["guide_rulings"], [fix])
        self.assertEqual(len(self.events("claims")), 1)
        (ruling,) = self.events("guide_ruling")
        self.assertEqual((ruling["choice"], ruling["role"], ruling["applied"]),
                         ("continue", "claims", True))

    def test_ask_owner_leaves_the_card_waiting_as_a_needs_you_item(self):
        self.stale_card()
        self.script["guide"] = [[decision("ask-owner", "only the owner can say")]]
        self.run_ticks(5)
        self.assertEqual((self.task()["status"], self.task()["block_kind"]), ("triage", "claims"))
        self.assertEqual(self.runs("implementer"), [])
        self.assertTrue(any("held (claims check)" in line and "the guide says ask-owner" in line
                            for line in self.outbox()))

    def test_a_card_held_once_is_never_held_again(self):
        self.stale_card()
        self.tick.once()
        blocks.reset_failures(self.conn, self.tid, by="human", now=int(time.time()))
        self.assertEqual(self.task()["status"], "ready")
        self.assertEqual(self.tick.once()["dispatched"], 1)
        self.assertEqual(len(self.events("claims")), 1)

    def test_no_claims_leaves_dispatch_unchanged(self):
        self.set_body("Plain prose with an `unclosed tick and app/missing.py in the open.")
        self.script["implementer"] = [[IMPL_DONE]]
        self.assertEqual(self.tick.once()["dispatched"], 1)
        self.assertEqual(self.events("claims"), [])

    def test_an_unreadable_repo_changes_nothing_and_logs_one_line(self):
        self.set_body("Fix `app/stale_note.py`.")
        row = self.conn.execute("SELECT * FROM tasks WHERE id=?", (self.tid,)).fetchone()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            held = claims.precheck(self.conn, row, self.home / "no-such-clone", 1)
        self.assertFalse(held)
        self.assertEqual(self.task()["status"], "ready")
        self.assertEqual(len(err.getvalue().splitlines()), 1)
        self.assertIn(self.tid, err.getvalue())

    def test_a_database_error_is_also_warn_safe(self):
        row = {"id": self.tid, "title": "t", "body": "`app/x.py`"}
        conn = mock.Mock()
        conn.execute.side_effect = sqlite3.OperationalError("database is locked")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertFalse(claims.precheck(conn, row, self.home, 1))
        self.assertIn("database is locked", err.getvalue())


PROTECTED_TARGET = "tests/mutation_check.py"


class ProtectedTarget(CardCase):
    def file_set_body(self):
        return f"Change the gate.\n\nFile set:\n- {PROTECTED_TARGET}\n- capsule/claims.py\n"

    def parked_events(self):
        return self.events("parked_by_human")

    def test_a_card_whose_file_set_names_a_protected_path_parks_before_dispatch(self):
        self.set_body(self.file_set_body())
        self.script["implementer"] = [[IMPL_DONE]]
        self.assertEqual(self.tick.once()["dispatched"], 0)
        task = self.task()
        self.assertEqual((task["status"], task["block_kind"]), ("blocked", "parked"))
        self.assertEqual(self.runs("implementer"), [])
        (ev,) = self.parked_events()
        self.assertIn(PROTECTED_TARGET, ev["trigger"])
        self.assertIn("row 395", ev["trigger"])
        self.assertEqual(self.events("claims"), [])

    def test_a_protected_path_named_only_in_prose_still_dispatches(self):
        self.set_body(f"Fix the bug.\n\nFile set:\n- capsule/claims.py\n\n## Incident\n"
                      f"Change `{PROTECTED_TARGET}` main().\n")
        self.script["implementer"] = [[IMPL_DONE]]
        self.assertEqual(self.tick.once()["dispatched"], 1)
        self.assertEqual(self.parked_events(), [])

    def test_a_card_with_earlier_runs_parks_on_its_next_dispatch(self):
        self.conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, ended_at)"
                          " VALUES (?,?,?,?,?)", (self.tid, "implementer", "crashed", 10, 70))
        self.set_body(self.file_set_body())
        self.assertEqual(self.tick.once()["dispatched"], 0)
        self.assertEqual((self.task()["status"], self.task()["block_kind"]), ("blocked", "parked"))
        self.assertEqual(len(self.parked_events()), 1)

    def test_a_bare_root_protected_name_in_the_file_set_parks(self):
        self.set_body("Change the deploy.\n\nFile set:\n- deploy.sh\n")
        self.script["implementer"] = [[IMPL_DONE]]
        self.assertEqual(self.tick.once()["dispatched"], 0)
        (ev,) = self.parked_events()
        self.assertIn("deploy.sh", ev["trigger"])

    def test_an_unreadable_body_stays_warn_safe(self):
        row = {"id": self.tid, "title": "t", "body": self.file_set_body()}
        err = io.StringIO()
        with mock.patch("capsule.claims.risk.file_set", side_effect=ValueError("boom")), \
                contextlib.redirect_stderr(err):
            self.assertFalse(claims.precheck(self.conn, row, self.home, 1))
        self.assertEqual(self.task()["status"], "ready")
        self.assertEqual(len(err.getvalue().splitlines()), 1)
        self.assertIn("boom", err.getvalue())


# Z1, contracts/intent/claims_precheck.jsonl: what each example's case must observe. The cases
# run on these built-in expectations alone, so they hold before (and without) the rows file.
EXPECT = {
    "path-deleted-at-branch-head": {"status": "triage", "block_kind": "claims",
                                    "missing_names_path": True, "implementer_runs": 0},
    "creates-or-unknown-folder": {"held": False, "implementer_runs": 1},
    "guide-rules": {"continue_implementer_runs": 1, "continue_claims_events": 1,
                    "continue_correction_reaches_implementer": True,
                    "ask_owner_status": "triage", "ask_owner_needs_you": True,
                    "ask_owner_implementer_runs": 0},
}


class IntentCases(CardCase):
    def fresh(self):
        self.tearDown()
        self.setUp()

    def deleted_at_branch_head(self):
        self.commit_to_origin("static/index.html", "<html></html>\n")
        self.commit_to_origin("static/app.js", "x = 1\n")
        origin = self.home / "origin"
        git(origin, "rm", "-q", "static/index.html")
        git(origin, "commit", "-qm", "index.html is gone")
        self.set_body("- Compatibility: `static/index.html` uses each string as an option.")
        self.script["implementer"] = [[IMPL_DONE]]
        self.tick.once()
        task, (ev, *_) = self.task(), self.events("claims") or [{"missing": []}]
        return {"status": task["status"], "block_kind": task["block_kind"],
                "missing_names_path": "static/index.html" in "".join(ev["missing"]),
                "implementer_runs": len(self.runs("implementer"))}

    def creates_or_unknown_folder(self):
        self.commit_to_origin("app/timer.py", TIMER_PY)
        self.set_body("- Files: create `app/screens/pet.py`\n"
                      "- [ ] Tests first, `app/screens/pet.py`\n"
                      "Notes live in `scratchpad/plan.md`; wire it into `app/timer.py`.")
        self.script["implementer"] = [[IMPL_DONE]]
        self.tick.once()
        return {"held": bool(self.events("claims")),
                "implementer_runs": len(self.runs("implementer"))}

    def guide_rules(self):
        fix = "`app/stale_note.py` is now `app/timer.py`; edit that one"
        self.stale_card()
        self.script["guide"] = [[decision("continue", fix)], [GUIDE_OK]]
        self.run_ticks(6)
        runs = self.runs("implementer")
        spool = self.spool_dir(runs[0]["run_id"]) if runs else None
        job = json.loads((spool / "job.json").read_text()) if spool else {}
        seen = {"continue_implementer_runs": len(runs),
                "continue_claims_events": len(self.events("claims")),
                "continue_correction_reaches_implementer":
                    job.get("inputs", {}).get("guide_rulings") == [fix]}
        self.fresh()
        self.stale_card()
        self.script["guide"] = [[decision("ask-owner", "only the owner can say")]]
        self.run_ticks(5)
        asked = any("the guide says ask-owner" in line for line in self.outbox())
        return dict(seen, ask_owner_status=self.task()["status"], ask_owner_needs_you=asked,
                    ask_owner_implementer_runs=len(self.runs("implementer")))

    def replay(self, case):
        self.fresh()
        return {"path-deleted-at-branch-head": self.deleted_at_branch_head,
                "creates-or-unknown-folder": self.creates_or_unknown_folder,
                "guide-rules": self.guide_rules}[case]()

    def test_each_case_observes_its_built_in_expectation(self):
        for case, want in EXPECT.items():
            self.assertEqual(self.replay(case), want, case)

    def test_the_shipped_rows_match_what_the_cases_observe(self):
        rows = intent.load("claims_precheck")
        if not rows:
            self.skipTest("contracts/intent/claims_precheck.jsonl is not shipped yet")
        self.assertEqual(len(rows), 3)
        for ex in rows:
            seen = self.replay(ex["given"]["document"]["case"])
            for key, value in ex["expect"].items():
                self.assertEqual(seen[key], value, f"{ex['id']}: {key}")


if __name__ == "__main__":
    unittest.main()
