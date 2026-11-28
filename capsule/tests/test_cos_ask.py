"""Notification rebuild, slice 3 (spec 2026-10-29, row 637): `capsule cos ask`, the plain-English
inbox message. Two oracles: the literal messages below (and the spec's own two worked examples,
read from the spec file) against the message the verb queues, and the ask file on disk against
the choices the owner would be offered. Nothing here reaches Slack: the verb only queues rows."""
import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from capsule import cos_ask, db, outbox, paths, slack
from capsule.tests.public_tree import private_only
from capsule.tests.private_names import retired_names
from capsule.__main__ import main

BOARD = "capsule-corp"
BRANCH = "epic/my-app-screens-epic"
NOW = 1_800_000_000
SPEC = (paths.REPO / "docs" / "superpowers" / "specs"
        / "2026-10-29-cos-role-and-notifications-design.md")

EXAMPLE_1 = (
    "\U0001F4EC Retry limit: count per card?\n"
    "Capsule Corp · Retry on rate limit\n"
    "What: A builder needs one rule settled to finish. Should the retry limit count all attempts"
    " on the card together, or each run separately?\n"
    "Why it matters: Per card stops a stuck card burning money. Per run lets a flaky run recover.\n"
    "If yes: one limit for the whole card.   If no: each run gets its own limit.\n"
    "My pick: Yes, because it is the cheaper mistake and you can flip it later.\n"
    "[ Yes ]  [ No ]  [ Later ]")
EXAMPLE_2 = (
    "\U0001F4EC Let a big job go ahead?\n"
    "Capsule Corp · Search page\n"
    "What: This job touches more files than the usual size limit, so it is on hold until you"
    " decide.\n"
    "Why it matters: Big jobs are harder to review and more likely to fail. Splitting it costs"
    " time.\n"
    "If yes: it goes ahead as it is.   If no: it stays on hold until it is split.\n"
    "My pick: No, split it, because the limit exists for a reason.\n"
    "[ Yes ]  [ No ]  [ Later ]")
ASK_1 = ["--title", "Retry limit: count per card?",
         "--what", "A builder needs one rule settled to finish. Should the retry limit count all"
                   " attempts on the card together, or each run separately?",
         "--why", "Per card stops a stuck card burning money. Per run lets a flaky run recover.",
         "--if-yes", "one limit for the whole card.", "--if-no", "each run gets its own limit.",
         "--pick", "Yes", "--because", "it is the cheaper mistake and you can flip it later",
         "--yes-option", "b", "--no-option", "a"]
ASK_2 = ["--title", "Let a big job go ahead?",
         "--what", "This job touches more files than the usual size limit, so it is on hold"
                   " until you decide.",
         "--why", "Big jobs are harder to review and more likely to fail. Splitting it costs time.",
         "--if-yes", "it goes ahead as it is.", "--if-no", "it stays on hold until it is split.",
         "--pick", "No, split it", "--because", "the limit exists for a reason",
         "--no-reason", "file ceiling"]


def spec_example(n):
    """The n-th worked example's message block from the spec, with its hard-wrapped lines joined."""
    text = SPEC.read_text(encoding="utf-8")
    block = text.split(f"Worked example {n}", 1)[1].split("```\n", 2)[1]
    out = []
    for i, line in enumerate(block.splitlines()):
        if i < 2 or line.startswith(("What:", "Why it matters:", "If yes:", "My pick:", "[ ")):
            out.append(line)
        else:
            out[-1] += " " + line
    return "\n".join(out)


class CosAsk(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.cfg = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        self.cfg.update(board=BOARD)
        self.cfg["slack"] = dict(self.cfg["slack"], enabled=True)
        self.cfg["slack"]["channels"] = dict(self.cfg["slack"]["channels"], inbox="#cc-inbox")
        paths.board_dir(BOARD, self.home).mkdir(parents=True)
        db.create_board(paths.board_db(BOARD, self.home))
        self.conn = db.connect(paths.board_db(BOARD, self.home))
        self.addCleanup(self.conn.close)
        self.write_cfg()
        self.blocked = self.card("Retry on rate limit", "blocked")
        self.decide(self.blocked, [("a", "per run"), ("b", "per card")])
        self.triage = self.card("Search page", "triage", branch=None, assignee=None)

    def decide(self, tid, options):
        """The decision.v1 a worker filed: what `spool` records as a `decision_requested` event."""
        doc = {"question": "Count per run or per card?", "recommended": options[-1][0],
               "options": [{"id": i, "label": label} for i, label in options]}
        db.event(self.conn, tid, "decision_requested", doc, now=NOW + 1)
        self.conn.commit()

    def write_cfg(self):
        paths.config_path(self.home, BOARD).write_text(json.dumps(self.cfg))
        outbox.set_slack(self.conn, slack.block(self.cfg), now=NOW)     # the tick's own mirror
        self.conn.commit()

    def card(self, name, status, branch=BRANCH, assignee="dev-backend"):
        tid = db.new_task(self.conn, title=f"{name} (long title)", short_name=name,
                          assignee=assignee, branch=branch, now=NOW)
        self.conn.execute("UPDATE tasks SET status=?, branch_name=?, assignee=? WHERE id=?",
                          (status, branch, assignee, tid))
        db.event(self.conn, tid, "blocked", {"reason": "x"}, now=NOW)
        self.conn.commit()
        return tid

    def ask(self, tid, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(["cos", "ask", BOARD, tid, *argv, "--capsule-home", str(self.home)])
        return rc, out.getvalue(), err.getvalue()

    def sent(self):
        return self.conn.execute("SELECT channel, body FROM outbox ORDER BY id").fetchall()

    def posted(self):
        return [r["body"] for r in self.sent() if r["channel"] == "slack"]

    def ask_files(self):
        return sorted((paths.board_dir(BOARD, self.home) / "asks").glob("*.json"))

    def assert_refused(self, rc, err, why):
        self.assertEqual(rc, 1)
        self.assertIn("refused", err)
        self.assertRegex(err, why)
        self.assertEqual((self.sent(), self.ask_files()), ([], []))      # nothing queued or written

    # -- the two worked examples (spec section 3) -------------------------------------------

    def test_example_1_renders_byte_equal_and_both_answers_unblock(self):
        rc, out, err = self.ask(self.blocked, ASK_1)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.posted(), [EXAMPLE_1])
        doc = json.loads(self.ask_files()[0].read_text())
        self.assertEqual(doc["choices"], {
            "yes": {"verb": "unblock",
                    "text": "option b chosen (the question's own option b: \"per card\")"},
            "no": {"verb": "unblock",
                   "text": "option a chosen (the question's own option a: \"per run\")"}})
        self.assertEqual(out, f"asked: needs:ask-{doc['key']}\n")

    def test_a_blocked_card_with_no_question_behind_it_gets_a_plain_yes_and_a_parking_no(self):
        plain = self.card("Plain hold", "blocked")
        argv = [a for a in ASK_1[:-4]] + ["--no-reason", "not now"]
        self.assertEqual(self.ask(plain, argv)[0], 0)
        doc = json.loads(self.ask_files()[0].read_text())
        self.assertEqual(doc["choices"],
                         {"yes": {"verb": "unblock",
                                  "text": "owner said yes to: Retry limit: count per card?"},
                          "no": {"verb": "park", "text": "owner said no: not now"}})
        self.conn.execute("DELETE FROM outbox_key")
        self.conn.execute("DELETE FROM outbox")
        rc, _, err = self.ask(plain, ASK_1[:-4])                      # a No needs its reason
        self.assertEqual((rc, self.posted()), (1, []))
        self.assertIn("--no-reason", err)
        rc, _, err = self.ask(plain, [*ASK_1, "--no-reason", "x"])    # no options to point at
        self.assertEqual((rc, self.posted()), (1, []))
        self.assertIn("no question with options", err)

    def test_option_ids_must_be_the_questions_own(self):
        for yes, no in (("z", "a"), ("b", "z"), ("b", "b")):
            with self.subTest(yes=yes, no=no):
                argv = [*ASK_1[:-4], "--yes-option", yes, "--no-option", no]
                self.assert_refused(*self.ask(self.blocked, argv)[::2], "option ids")
        self.decide(self.blocked, [("x", "later, newer question")])      # the latest one rules
        self.assert_refused(*self.ask(self.blocked, ASK_1)[::2], "option ids")

    def test_a_question_the_card_already_moved_past_is_not_offered(self):
        """followups 77: a decision_requested older than the last block-close event is stale."""
        db.event(self.conn, self.blocked, "unblocked", {}, now=NOW + 2)
        db.event(self.conn, self.blocked, "blocked", {"reason": "again"}, now=NOW + 3)
        self.conn.commit()
        self.assert_refused(*self.ask(self.blocked, ASK_1)[::2], "no question with options")
        self.decide(self.blocked, [("b", "per card"), ("a", "per run")])   # a fresh question works
        self.assertEqual(self.ask(self.blocked, ASK_1)[0], 0)

    def test_a_parked_card_is_not_offered_an_unblock(self):
        self.conn.execute("UPDATE tasks SET block_kind='parked' WHERE id=?", (self.blocked,))
        self.conn.commit()
        self.assert_refused(*self.ask(self.blocked, ASK_1)[::2], "parked")

    def test_example_2_renders_byte_equal_and_a_no_parks(self):
        rc, _, err = self.ask(self.triage, ASK_2)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.posted(), [EXAMPLE_2])
        doc = json.loads(self.ask_files()[0].read_text())
        self.assertEqual(doc["choices"],
                         {"yes": {"verb": "reset",
                                  "text": "owner said yes to: Let a big job go ahead?"},
                          "no": {"verb": "park", "text": "owner said no: file ceiling"}})

    @private_only
    def test_the_literals_are_the_spec_s_own_examples(self):
        self.assertEqual(spec_example(1), EXAMPLE_1)
        self.assertEqual(spec_example(2), EXAMPLE_2)

    # -- the id lint ------------------------------------------------------------------------

    def test_the_lint_refuses_ids_paths_and_commands_in_every_field(self):
        bad = ["t_ab12cd34", "inc_0a1b2c3d", "/home/example/notes", "capsule/outbox.py", "~/notes",
               "intake.py", "https://example.com/x", "python3 -m capsule", "`reset`",
               "capsule human x", "git push now",
               # the misses a review found: an id glued to a word, repo paths, a glob, a db file,
               # a bare short sha and a long hex run
               "Card_t_ab12cd34", "capsule/outbox", "docs/RUNBOOK", "contracts/**", "kanban.db",
               "5ef2bb4b", "ab12cd34ef"]
        for field in ("--title", "--what", "--why", "--if-yes", "--if-no", "--because",
                      "--card-name", "--project"):
            for text in bad:
                with self.subTest(field=field, text=text):
                    rc, _, err = self.ask(self.blocked, [*ASK_1, field, f"fix {text} soon"])
                    self.assert_refused(rc, err, "plain words")

    def test_plain_words_with_slashes_and_digits_pass(self):
        argv = [*ASK_1, "--what", "Pick yes/no, and/or 1/2 of the 3 runs, 24/7; set_value, a"
                " deadbeef and 12345678 are fine."]
        self.assertEqual(self.ask(self.blocked, argv)[0], 0)

    def test_a_retired_system_dir_is_no_longer_a_repo_path(self):
        for name in retired_names():
            self.assertNotIn(name, cos_ask.REPO_DIRS.split("|"))
            self.assertIsNone(cos_ask.PATH_RE.search(f"fix {name}/gateway soon"))

    def test_every_field_is_capped_and_so_is_the_whole_message(self):
        self.assert_refused(*self.ask(self.blocked, [*ASK_1, "--why", "word " * 61])[::2],
                            "longer than 300")
        self.assertEqual(self.ask(self.blocked, [*ASK_1, "--why", ("w" * 299)])[0], 0)
        slack.render("ask", "title\n" + "x" * 2990)                     # at the cap: fine
        with self.assertRaisesRegex(ValueError, "body over 3000"):
            slack.render("ask", "title\n" + "x" * 3000)

    def test_a_value_cannot_add_a_line_of_its_own(self):
        rc, _, _ = self.ask(self.blocked, [*ASK_1, "--what", "one.\nMy pick: No, because x."])
        self.assertEqual((rc, len(self.posted()[0].splitlines())), (0, 7))

    def test_bad_input_is_refused(self):
        for argv, why in ((["--title", "one two three four five six seven eight nine"], "title"),
                          (["--pick", "Maybe"], "Yes or No"), (["--what", "  "], "empty field")):
            with self.subTest(argv=argv):
                rc, _, err = self.ask(self.blocked, [*ASK_1, *argv])
                self.assert_refused(rc, err, why)
        for argv, why in (([*ASK_1[:-4]], "option ids"),
                          ([*ASK_1[:-2], "--no-option", "b"], "option ids")):
            with self.subTest(argv=argv):
                self.assert_refused(*self.ask(self.blocked, argv)[::2], why)

    # -- the inbox key ----------------------------------------------------------------------

    def test_with_no_inbox_key_nothing_posts(self):
        self.cfg["slack"]["channels"].pop("inbox")
        for routing in (None, "v2"):
            self.cfg["slack"].pop("routing", None)
            self.cfg["slack"].update({"routing": routing} if routing else {})
            self.write_cfg()
            with self.subTest(routing=routing):
                rc, _, err = self.ask(self.blocked, ASK_1)
                self.assert_refused(rc, err, "inbox")

    def test_the_ask_posts_under_its_key_in_the_inbox_whatever_the_routing(self):
        for routing in (None, "v2"):
            self.conn.execute("DELETE FROM outbox_key")
            self.conn.execute("DELETE FROM outbox")
            self.cfg["slack"].update({"routing": routing} if routing else {})
            self.write_cfg()
            with self.subTest(routing=routing):
                rc, out, _ = self.ask(self.blocked, ASK_1)
                self.assertEqual(rc, 0)
                key = out.split("needs:ask-")[1].strip()
                self.assertEqual([r["channel"] for r in self.sent()], ["file", "slack"])
                row = self.conn.execute("SELECT o.body, k.dedup_key FROM outbox o JOIN outbox_key k"
                                        " ON k.outbox_id=o.id").fetchone()
                self.assertEqual(row["dedup_key"], f"needs:ask-{key}")
                self.assertEqual(slack.classify(row["body"]), "ask")
                self.assertEqual(slack.route(self.cfg["slack"], row["body"]), "#cc-inbox")

    def test_slack_off_for_the_board_refuses(self):
        self.cfg["slack"]["enabled"] = False
        self.write_cfg()
        self.assert_refused(*self.ask(self.blocked, ASK_1)[::2], "inbox")
        self.cfg["slack"]["enabled"] = True
        self.write_cfg()
        db.guard_set(self.conn, outbox.SLACK_FLAG, False, now=NOW)     # not mirrored by the tick
        self.assert_refused(*self.ask(self.blocked, ASK_1)[::2], "not on for this board")

    def test_the_inbox_key_is_validated_and_page_and_ask_have_no_key_of_their_own(self):
        channels = self.cfg["slack"]["channels"]
        self.assertIs(slack.validate(self.cfg["slack"]), self.cfg["slack"])
        for bad in (dict(channels, inbox=""), dict(channels, inbox=7), dict(channels, page="#p")):
            with self.subTest(channels=bad), self.assertRaises(ValueError):
                slack.validate(dict(self.cfg["slack"], channels=bad))

    # -- the ask file (spec section 4) ------------------------------------------------------

    def test_the_ask_file_holds_what_the_listener_needs(self):
        db.event(self.conn, self.blocked, "blocked", {"reason": "again"}, now=NOW + 2)
        self.conn.commit()
        last = self.conn.execute("SELECT MAX(id) FROM task_events WHERE task_id=?",
                                 (self.blocked,)).fetchone()[0]
        self.ask(self.blocked, ASK_1)
        self.ask(self.triage, ASK_2)
        files = {json.loads(p.read_text())["task_id"]: json.loads(p.read_text())
                 for p in self.ask_files()}
        doc = files[self.blocked]
        self.assertEqual((doc["board"], doc["after_event_id"], doc["expected_status"]),
                         (BOARD, last, "blocked"))
        self.assertEqual((doc["title"], doc["asked_by"], doc["block_kind"]),
                         ("Retry limit: count per card?", "cos", None))
        self.assertEqual(set(doc), {"key", "title", "task_id", "asked_by", "board",
                                    "after_event_id", "expected_status", "block_kind",
                                    "choices", "at"})       # slice 4 and the listener read these
        self.assertEqual(files[self.triage]["expected_status"], "triage")
        self.assertEqual({k: set(v) for k, v in doc["choices"].items()},
                         {"yes": {"verb", "text"}, "no": {"verb", "text"}})
        keys = {d["key"] for d in files.values()}
        self.assertEqual(len(keys), 2)                         # random per ask, never a card id
        for key in keys:
            self.assertRegex(key, r"^[0-9a-f]{16}$")
        self.assertTrue(all(d["key"] + ".json" in {p.name for p in self.ask_files()}
                            for d in files.values()))

    # -- the reset check ----------------------------------------------------------------------

    def test_an_unroutable_reset_is_refused_before_it_is_offered(self):
        self.cfg["epics"] = [BRANCH, "epic/other"]
        self.write_cfg()
        rc, _, err = self.ask(self.triage, ASK_2)
        self.assert_refused(rc, err, "reset Yes would do nothing: epic missing")
        stuck = self.card("Odd one", "triage", branch=BRANCH, assignee="nobody")
        rc, _, err = self.ask(stuck, ASK_2)
        self.assert_refused(rc, err, "assignee missing")

    def test_a_routable_reset_is_offered_and_a_card_in_another_state_is_refused(self):
        self.assertEqual(self.ask(self.triage, ASK_2)[0], 0)          # one declared epic: routable
        self.conn.execute("DELETE FROM outbox_key")
        self.conn.execute("DELETE FROM outbox")
        running = self.card("Busy", "running")
        rc, _, err = self.ask(running, ASK_2)
        self.assertEqual((rc, self.posted()), (1, []))
        self.assertIn("running", err)
        rc, _, err = self.ask(self.triage, ASK_2[:-2])         # no --no-reason: nothing to record
        self.assertEqual((rc, self.posted()), (1, []))
        self.assertIn("--no-reason", err)
