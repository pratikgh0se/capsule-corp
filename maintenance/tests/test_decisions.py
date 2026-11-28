# mutation-check: targets-predate-range b957739c9 -- rename only, no behaviour pinned ::
#   BoardSeam.test_kanban_calls_pin_the_board_they_name
import os, sys, json, unittest, unittest.mock, tempfile, subprocess, datetime as dt
from pathlib import Path
os.environ["AGENT_OPS_NO_SEND"] = "1"
sys.path[:0] = [str(Path(__file__).resolve().parents[n]) for n in (1, 2)]
import capsule.tests  # noqa: E402,F401 - its __init__ installs the swept TMPDIR (tmpclean)
os.environ.setdefault("EVENTS_SPOOL", tempfile.mkdtemp(prefix="events-dec-") + "/events.jsonl")
os.environ.setdefault("MERGE_GATE_STATE", tempfile.mkdtemp(prefix="gate-state-dec-"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import decisions as dc
from decisions import NOTES_HINT
from fakes import block_shapes, blocked_event

BLOCK = ("DECISION: Which HTTP client for the poller?\n"
         "Why: adds a dependency the whole repo will carry\n"
         "1. urllib (stdlib) - no dependency, verbose code\n"
         "2. httpx - clean API, one more package to vendor\n"
         "3. requests - familiar, sync only\n"
         "Recommend: 1 - the repo is stdlib-only by rule; the verbosity is 20 lines\n"
         "Learn: how much of this codebase is deliberately dependency-free\n")
from capsule.tests.public_tree import private_only  # noqa: E402


class Parse(unittest.TestCase):
    def test_parse(self):
        d = dc.parse_decision(BLOCK)
        self.assertEqual(d["question"], "Which HTTP client for the poller?")
        self.assertEqual(d["options"][1], ("httpx", "clean API, one more package to vendor"))
        self.assertEqual((d["recommend"], d["brainstorm"]), (1, False))
        self.assertTrue(d["reasoning"].startswith("the repo is stdlib-only"))
        self.assertEqual(dc.parse_decision(BLOCK + "Brainstorm: yes\n")["brainstorm"], True)
    def test_em_and_en_dash_separators_parse(self):
        # U1 incident 1 (2026-09-13, t_d6353cd0): the worker wrote the option
        # separator as an em-dash and the gate rejected a 4-option DECISION.
        block = BLOCK.replace(" - ", " \u2014 ", 3).replace("Recommend: 1 - ", "Recommend: 1 \u2013 ")
        d = dc.parse_decision(block)
        self.assertEqual(len(d["options"]), 3)
        self.assertEqual(d["options"][1], ("httpx", "clean API, one more package to vendor"))
        self.assertEqual(d["recommend"], 1)
        self.assertTrue(d["reasoning"].startswith("the repo is stdlib-only"))

    def test_not_a_decision_and_malformed(self):
        self.assertIsNone(dc.parse_decision("BLOCKER: cannot run tests"))
        with self.assertRaises(ValueError): dc.parse_decision("DECISION: q\nWhy: w\n1. a - b\nRecommend: 1 - r\nLearn: l")   # one option
        with self.assertRaises(ValueError): dc.parse_decision(BLOCK.replace("Recommend: 1", "Recommend: 9"))
        with self.assertRaises(ValueError): dc.parse_decision(BLOCK.replace("Recommend: 1 - the repo is stdlib-only by rule; the verbosity is 20 lines\n", ""))

    def test_slug_and_path(self):
        self.assertEqual(dc.slugify("Which HTTP client for the poller?"), "which-http-client-for-the-poller")
        self.assertEqual(str(dc.note_path(Path("/v"), "my-app", "2026-09-06", "x", "t_abc")),
                         "/v/Decisions/my-app/2026-09-06-x-t_abc.md")

    def test_slugify_empty(self):
        self.assertEqual(dc.slugify(""), "untitled")
        self.assertEqual(dc.slugify("???"), "untitled")

    def test_continuation_lines(self):
        block = ("DECISION: Which HTTP client for the poller?\n"
                  "Why: adds a dependency the whole\n"
                  "repo will carry\n"
                  "1. urllib - no dependency,\n"
                  "verbose code but stdlib\n"
                  "2. httpx - clean API, one more package to vendor\n"
                  "Recommend: 1 - the repo is stdlib-only by rule\n"
                  "Learn: how much of this codebase is deliberately dependency-free\n")
        d = dc.parse_decision(block)
        self.assertEqual(d["why"], "adds a dependency the whole repo will carry")
        self.assertEqual(d["options"][0], ("urllib", "no dependency, verbose code but stdlib"))


class Note(unittest.TestCase):
    def test_render_and_read(self):
        d = dc.parse_decision(BLOCK)
        txt = dc.render_note(d, board="my-app", card="t_abc", raised_by="dev-backend",
                             repo="my-app", date="2026-09-06", title="Poll the API")
        self.assertTrue(txt.startswith(
            "---\ndate: 2026-09-06\ntype: decision\nstatus: open\nboard: my-app\ncard: t_abc\n"
            "raised_by: dev-backend\nclass: R3\ntags: [decision, my-app]\nai-first: true\n---\n"))
        for h in ("## For future agent", "## Question", "## Options", "## Your answer", "### Notes", "**Recommendation:** 1 - ", "**What you learn:**"):
            self.assertIn(h, txt)
        self.assertIn("- [ ] 1. urllib (stdlib)\n- [ ] 2. httpx\n- [ ] 3. requests\n- [ ] Other: (write it in the notes)\n", txt)
        r = dc.read_note(txt)
        self.assertEqual((r["fm"]["status"], r["ticked"], r["notes"], r["options"]), ("open", None, "", ["urllib (stdlib)", "httpx", "requests"]))
        ticked = txt.replace("- [ ] 2. httpx", "- [x] 2. httpx").replace("<!-- anything you write here is attached to the card verbatim for the worker -->", "go with httpx, vendor it")
        r = dc.read_note(ticked)
        self.assertEqual((r["ticked"], r["notes"]), (2, "go with httpx, vendor it"))
        self.assertEqual(dc.read_note(txt.replace("- [ ] Other:", "- [x] Other:"))["ticked"], "other")
        with self.assertRaises(ValueError): dc.read_note(ticked.replace("- [ ] 1. urllib", "- [x] 1. urllib"))

    def test_read_note_ignores_ticks_in_notes(self):
        d = dc.parse_decision(BLOCK)
        txt = dc.render_note(d, board="my-app", card="t_abc", raised_by="dev-backend",
                             repo="my-app", date="2026-09-06", title="Poll the API")
        stray = txt.replace(NOTES_HINT, "- [x] 2. remember the changelog")
        r = dc.read_note(stray)
        self.assertEqual((r["ticked"], r["notes"]), (None, "- [x] 2. remember the changelog"))
        both = stray.replace("- [ ] 3. requests", "- [x] 3. requests")
        r = dc.read_note(both)
        self.assertEqual(r["ticked"], 3)


def _block_records(status, reason, comments, events):
    """Fixtures here describe cards a WORKER blocked, and a worker blocks
    through the `kanban_block` MCP tool: no comment, one `blocked` task_event
    carrying the reason (tools/kanban_tools.py:866-871 -> kanban_db.py:6464).
    So a fixture that only sets `reason`/`latest_summary` gets the event a
    real `kanban_block(reason=...)` would have written - unless the test
    supplied its own comments or events (then trust those verbatim)."""
    comments, events = list(comments or []), list(events or [])
    already = events or any(str(c.get("body") or "").startswith("BLOCKED: ") for c in comments)
    if status in ("blocked", "triage") and reason and not already:
        events = [blocked_event(reason, at=0)]
    return comments, events


class FakeKanban:
    def __init__(self, cards):
        self.cards, self.calls = cards, []
    def __call__(self, argv):
        self.calls.append(argv)
        if "list" in argv:
            return json.dumps([{"id": k, "assignee": v["assignee"], "status": v["status"], "title": v["title"]} for k, v in self.cards.items()])
        if "show" in argv:
            c = self.cards[argv[argv.index("show") + 1]]
            reason = c.get("reason", "")
            comments, events = _block_records(c["status"], reason, c.get("comments", []), c.get("events", []))
            return json.dumps({"task": {"id": argv[argv.index("show") + 1], "assignee": c["assignee"], "status": c["status"], "title": c["title"], "workspace_path": c.get("ws", ""), "body": c.get("body", "")},
                               "latest_summary": reason, "comments": comments, "events": events, "parents": [], "children": [], "runs": []})
        return ""


class UnblockFailsKanban(FakeKanban):
    """comment succeeds, unblock raises - models a board call failing partway
    through answer()'s kanban step."""
    def __call__(self, argv):
        if "unblock" in argv:
            self.calls.append(argv)
            raise subprocess.CalledProcessError(1, argv)
        return super().__call__(argv)


def boards_with(*names):
    b = Path(tempfile.mkdtemp(prefix="boards-dec-"))
    for n in names:
        (b / n).mkdir(); (b / n / "kanban.db").write_bytes(b"")
    return b


class Answer(unittest.TestCase):
    def setUp(self):
        self.vault = Path(tempfile.mkdtemp(prefix="vault-ans-")); (self.vault / "Specs").mkdir()
        self.cards = {"t_dec": {"assignee": "dev-backend", "status": "blocked", "title": "Poll the API", "reason": BLOCK, "ws": "/w",
                                "body": "<!-- sprint-meta\nrisk: low\nspec: Specs/poll-the-api.md\n-->"}}
        self.k = FakeKanban(self.cards)
        d = dc.parse_decision(BLOCK)
        slug = dc.slugify(d["question"])
        path = dc.note_path(self.vault, "my-app", "2026-09-06", slug, "t_dec")
        path.parent.mkdir(parents=True)
        path.write_text(dc.render_note(d, board="my-app", card="t_dec", raised_by="dev-backend",
                                       repo="my-app", date="2026-09-06", title="Poll the API"))
        self.note = dc.find_note(self.vault, "my-app", "t_dec")
        (self.vault / "Specs" / "poll-the-api.md").write_text("---\nslug: poll-the-api\n---\n## Goal\nx\n")

    def test_answer_by_number(self):
        out = dc.answer("my-app", "t_dec", "2", vault=self.vault, kanban=self.k,
                        now=dt.datetime(2026, 9, 6, 17, 0))
        self.assertEqual(out, "my-app t_dec: decided 2 - httpx")
        txt = self.note.read_text()
        self.assertIn("status: decided\n", txt); self.assertIn("decided: 2\n", txt); self.assertIn("## Decision\nhttpx", txt)
        self.assertIn("- [x] 2. httpx", txt)
        c = [a for a in self.k.calls if "comment" in a][-1][-1]
        self.assertEqual(c, "human: decision 2 - httpx / notes: ")
        self.assertIn("\nDecided (2026-09-06): httpx - ", (self.vault / "Specs" / "poll-the-api.md").read_text())
        self.assertTrue([a for a in self.k.calls if "unblock" in a])

    def test_done_uses_the_tick_and_notes(self):
        t = self.note.read_text().replace("- [ ] 1. urllib", "- [x] 1. urllib").replace(dc.NOTES_HINT, "keep it stdlib\nsecond line")
        self.note.write_text(t)
        out = dc.answer("my-app", "t_dec", "done", vault=self.vault, kanban=self.k)
        self.assertEqual(out, "my-app t_dec: decided 1 - urllib (stdlib)")
        c = [a for a in self.k.calls if "comment" in a][-1][-1]
        self.assertEqual(c, "human: decision 1 - urllib (stdlib) / notes: keep it stdlib\nsecond line")

    def test_conflict_and_nothing_ticked(self):
        t = self.note.read_text().replace("- [ ] 1. urllib", "- [x] 1. urllib"); self.note.write_text(t)
        with self.assertRaises(ValueError) as e:
            dc.answer("my-app", "t_dec", "2", vault=self.vault, kanban=self.k)
        self.assertIn("note says 1, reply says 2", str(e.exception))
        self.note.write_text(t.replace("- [x] 1. urllib", "- [ ] 1. urllib"))
        with self.assertRaises(ValueError):
            dc.answer("my-app", "t_dec", "done", vault=self.vault, kanban=self.k)
        self.assertFalse([a for a in self.k.calls if "unblock" in a])

    def test_other_with_text(self):
        out = dc.answer("my-app", "t_dec", "other", "use aiohttp, we go async anyway",
                        vault=self.vault, kanban=self.k)
        self.assertEqual(out, "my-app t_dec: decided other - use aiohttp, we go async anyway")

    def test_refuses_non_decision_card(self):
        self.cards["t_dec"]["reason"] = "READY: x"
        with self.assertRaises(ValueError):
            dc.answer("my-app", "t_dec", "1", vault=self.vault, kanban=self.k)
        self.assertFalse([a for a in self.k.calls if "unblock" in a])

    def test_refuses_a_card_whose_trail_is_not_a_decision_even_if_latest_summary_still_is(self):
        # Regression for the old bug: classification used to read
        # `latest_summary`, the board's newest-run-summary alias, which survives
        # unblock/reblock and is not block state. A card currently blocked on
        # something else entirely (trail: BLOCKER) must be refused even
        # though a stale `latest_summary` still reads DECISION: ...
        self.cards["t_dec"]["comments"] = [
            {"author": "legacy", "body": "BLOCKED: BLOCKER: waiting on infra", "created_at": 5}]
        with self.assertRaises(ValueError) as e:
            dc.answer("my-app", "t_dec", "1", vault=self.vault, kanban=self.k)
        self.assertIn("not blocked on a DECISION", str(e.exception))
        self.assertFalse([a for a in self.k.calls if "unblock" in a])

    def test_cli(self):
        import io, contextlib
        buf = io.StringIO()
        def fake(*a, **k):
            return "my-app t_dec: decided 1 - x"
        with unittest.mock.patch.object(dc, "answer", fake), contextlib.redirect_stdout(buf):
            dc.main(["answer", "my-app", "t_dec", "1"])
        self.assertIn("decided 1", buf.getvalue())

    def test_cli_reports_a_transition_refused_as_not_applied_exit_2(self):
        # Opus review blocker 2: main()'s except tuple only caught
        # (ValueError, CalledProcessError, OSError) - Board.unblock's
        # TransitionRefused (raised for a triage-parked card) is a
        # board.BoardError, not any of those, so it fell through to an
        # unhandled traceback instead of the ordinary "not applied - ..."
        # exit 2 every other answer() refusal gets.
        import io, contextlib
        buf = io.StringIO()
        with unittest.mock.patch.object(
                dc, "answer",
                side_effect=dc.board.TransitionRefused(
                    "my-app t_dec: cannot unblock from status 'triage'")), \
                contextlib.redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
            dc.main(["answer", "my-app", "t_dec", "1"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("not applied -", buf.getvalue())
        self.assertIn("triage", buf.getvalue())

    def test_unblock_failure_leaves_note_open_and_no_spec_line(self):
        k = UnblockFailsKanban(self.cards)
        with self.assertRaises(subprocess.CalledProcessError):
            dc.answer("my-app", "t_dec", "2", vault=self.vault, kanban=k)
        txt = self.note.read_text()
        self.assertIn("status: open\n", txt)
        self.assertNotIn("status: decided", txt)
        self.assertNotIn("Decided (", (self.vault / "Specs" / "poll-the-api.md").read_text())
        self.assertTrue([a for a in k.calls if "comment" in a])  # comment did go out
        self.assertTrue([a for a in k.calls if "unblock" in a])  # unblock was attempted

    def test_answer_on_a_triage_parked_decision_reports_clearly_and_leaves_note_open(self):
        # Live bug (2026-09-11): a second DECISION on the same card gets
        # loop-parked in `triage` by the board's block-loop detector
        # (kanban_db.py:~6406). The board has no CLI verb that resumes a
        # `triage` card, so Board.unblock reacts to the CLI's refusal (it
        # does not pre-check status - see its own docstring) and re-raises
        # as board.TransitionRefused - answer() must surface that distinctly,
        # not a bare CLI failure, while still posting the human's answer as
        # a comment and leaving the note open for a retry once the card is
        # manually recovered.
        self.cards["t_dec"]["status"] = "triage"
        k = UnblockFailsKanban(self.cards)  # models `kanban unblock` refusing a triage card
        with self.assertRaises(dc.board.TransitionRefused) as e:
            dc.answer("my-app", "t_dec", "2", vault=self.vault, kanban=k)
        self.assertIn("triage", str(e.exception))
        txt = self.note.read_text()
        self.assertIn("status: open\n", txt)
        self.assertNotIn("status: decided", txt)
        self.assertTrue([a for a in k.calls if "comment" in a])   # answer was recorded
        self.assertTrue([a for a in k.calls if "unblock" in a])   # unblock was attempted, then refused

    def test_other_multiline_text_collapses_spec_line_not_decision_section(self):
        text = "use aiohttp, we go async anyway\n## Goal\nsomething injected"
        out = dc.answer("my-app", "t_dec", "other", text, vault=self.vault, kanban=self.k)
        self.assertEqual(out, f"my-app t_dec: decided other - {text}")
        spec_txt = (self.vault / "Specs" / "poll-the-api.md").read_text()
        decided_lines = [l for l in spec_txt.splitlines() if l.startswith("Decided (")]
        self.assertEqual(len(decided_lines), 1)  # not split across lines by the embedded "## Goal" heading
        self.assertNotIn("\n", decided_lines[0])
        note_txt = self.note.read_text()
        self.assertIn(f"## Decision\n{text}", note_txt)  # full text kept in the note

    def test_other_comment_option_is_collapsed_to_one_line(self):
        text = "use aiohttp\nbecause it's fast"
        dc.answer("my-app", "t_dec", "other", text, vault=self.vault, kanban=self.k)
        c = [a for a in self.k.calls if "comment" in a][-1][-1]
        self.assertTrue(c.startswith("human: decision other - use aiohttp because it's fast / notes:"))

    def test_other_text_capped_at_300_chars_in_spec_line(self):
        text = "x" * 500
        dc.answer("my-app", "t_dec", "other", text, vault=self.vault, kanban=self.k)
        spec_txt = (self.vault / "Specs" / "poll-the-api.md").read_text()
        decided = [l for l in spec_txt.splitlines() if l.startswith("Decided (")][0]
        option_part, _, notes_part = decided.partition(": ")[2].partition(" - ")
        self.assertLessEqual(len(option_part), 300)
        self.assertLessEqual(len(notes_part), 300)

    def test_spec_path_traversal_is_skipped_not_erred(self):
        self.cards["t_dec"]["body"] = "<!-- sprint-meta\nrisk: low\nspec: Specs/../../escaped.md\n-->"
        out = dc.answer("my-app", "t_dec", "1", vault=self.vault, kanban=self.k)
        self.assertEqual(out, "my-app t_dec: decided 1 - urllib (stdlib)")
        self.assertFalse((self.vault.parent / "escaped.md").exists())
        self.assertNotIn("Decided (", (self.vault / "Specs" / "poll-the-api.md").read_text())

    def test_no_spec_in_body_skips_spec_line_no_error(self):
        self.cards["t_dec"]["body"] = "<!-- sprint-meta\nrisk: low\n-->"
        out = dc.answer("my-app", "t_dec", "1", vault=self.vault, kanban=self.k)
        self.assertEqual(out, "my-app t_dec: decided 1 - urllib (stdlib)")
        self.assertNotIn("Decided (", (self.vault / "Specs" / "poll-the-api.md").read_text())

    def test_double_reply_second_call_finds_no_open_note(self):
        dc.answer("my-app", "t_dec", "1", vault=self.vault, kanban=self.k)
        with self.assertRaises(ValueError) as e:
            dc.answer("my-app", "t_dec", "1", vault=self.vault, kanban=self.k)
        self.assertIn("no open decision note", str(e.exception))

    def test_two_open_notes_raises(self):
        twin = self.note.parent / self.note.name.replace("2026-09-06", "2026-09-05")
        twin.write_text(self.note.read_text())
        with self.assertRaises(ValueError) as e:
            dc.answer("my-app", "t_dec", "1", vault=self.vault, kanban=self.k)
        self.assertIn("two open decisions for t_dec", str(e.exception))
        self.assertIn("answer the older one first", str(e.exception))
        self.assertFalse([a for a in self.k.calls if "unblock" in a])

    def test_find_note_skips_unparsable_notes(self):
        bad = self.note.parent / "2026-09-04-bad-t_dec.md"
        # two ticks -> read_note raises ValueError while parsing; find_note must
        # skip it rather than blow up or count it as a second open note - a
        # valid open note (self.note) still wins.
        bad.write_text(self.note.read_text().replace("- [ ] 1. urllib", "- [x] 1. urllib").replace("- [ ] 2. httpx", "- [x] 2. httpx"))
        self.assertEqual(dc.find_note(self.vault, "my-app", "t_dec"), self.note)

    def test_answer_reports_which_note_is_unreadable(self):
        # I6: with no good open note but an unparsable candidate for this
        # task_id, answer() must say WHICH note is broken and why - not claim
        # there is no note at all.
        t = self.note.read_text().replace("- [ ] 1. urllib", "- [x] 1. urllib").replace("- [ ] 2. httpx", "- [x] 2. httpx")
        self.note.write_text(t)  # now unparsable: two ticks
        with self.assertRaises(ValueError) as e:
            dc.answer("my-app", "t_dec", "1", vault=self.vault, kanban=self.k)
        self.assertIn(f"cannot read the note {self.note.name}", str(e.exception))
        self.assertFalse([a for a in self.k.calls if "unblock" in a])

    def test_answer_rejects_bad_board(self):
        # I9: board is interpolated into a vault path; validate it independently
        # of the bash wrapper's regex.
        with self.assertRaises(ValueError) as e:
            dc.answer("../evil", "t_dec", "1", vault=self.vault, kanban=self.k)
        self.assertIn("bad board", str(e.exception))
        self.assertFalse([a for a in self.k.calls if "unblock" in a])

    def test_find_note_rejects_bad_board(self):
        with self.assertRaises(ValueError) as e:
            dc.find_note(self.vault, "-leading-hyphen", "t_dec")
        self.assertIn("bad board", str(e.exception))


class DeployRegexes(unittest.TestCase):
    @private_only
    def test_answer_decision_board_regex_rejects_leading_hyphen(self):
        script = Path(__file__).resolve().parents[2] / "bin" / "answer-decision.sh"
        self.assertIn('^[a-z0-9][a-z0-9-]*$', script.read_text())


if __name__ == "__main__":
    unittest.main()


class VaultPush(unittest.TestCase):
    """answer pulls first and
    pushes the note (and the spec line) last - the owner 2026-09-06: 'push from script'."""

    def test_answer_pulls_first_and_pushes_last(self):
        vault = Path(tempfile.mkdtemp(prefix="vault-push-"))
        (vault / "Decisions" / "my-app").mkdir(parents=True)
        d = dc.parse_decision("DECISION: Which?\nWhy: w\n1. a - b\n2. c - d\nRecommend: 1 - r\nLearn: l")
        p = dc.note_path(vault, "my-app", "2026-09-06", "which", "t_p3")
        p.write_text(dc.render_note(d, board="my-app", card="t_p3", raised_by="dev", repo="my-app",
                                  date="2026-09-06", title="T"))
        order = []
        def runner(cmd): order.append(("git", cmd[3])); return subprocess.CompletedProcess(cmd, 1 if cmd[3:5] == ["diff", "--cached"] else 0, "", "")
        def kb(cmd):
            args = cmd[cmd.index("kanban") + 3:]
            order.append(("kanban", args[0]))
            if args[0] == "show":
                reason = "DECISION: Which?\nWhy: w\n1. a - b\n2. c - d\nRecommend: 1 - r\nLearn: l"
                comments, events = _block_records("blocked", reason, [], [])
                return json.dumps({"task": {"id": "t_p3", "status": "blocked", "body": ""}, "latest_summary": reason,
                                   "comments": comments, "events": events, "parents": [], "children": [], "runs": []})
            return ""
        dc.answer("my-app", "t_p3", "2", vault=vault, kanban=kb, git_runner=runner)
        self.assertEqual(order[0], ("git", "pull"))
        self.assertIn(("kanban", "unblock"), order)
        self.assertEqual(order[-1], ("git", "push"))
        self.assertLess(order.index(("kanban", "unblock")), order.index(("git", "push")))


class BlockedDecisionsReadsTheBlockRecord(unittest.TestCase):
    """Plan gap 6: blocked_decisions() must classify from the card's live
    block reason, not latest_summary, same as _spec_for/guard - and from
    either of the board's two block paths (C2)."""

    def _runner(self, status, comments, latest_summary="", events=()):
        def kb(argv):
            if "list" in argv:
                return json.dumps([{"id": "t_1", "status": status, "assignee": "dev", "title": "T"}])
            if "show" in argv:
                return json.dumps({"task": {"id": "t_1", "assignee": "dev", "status": status,
                                            "title": "T", "workspace_path": "/w", "body": ""},
                                   "latest_summary": latest_summary, "comments": list(comments),
                                   "events": list(events),
                                   "parents": [], "children": [], "runs": []})
            return ""
        return kb

    def test_a_stale_decision_summary_is_not_treated_as_an_open_decision(self):
        # A blocked-on-something-else card (trail: BLOCKER) whose stale
        # latest_summary still reads DECISION: ... must not be reopened.
        kb = self._runner("blocked", [{"author": "h", "body": "BLOCKED: BLOCKER: waiting on infra",
                                       "created_at": 1}], latest_summary="DECISION: answered days ago")
        out = dc.blocked_decisions(kanban=kb, boards_dir=boards_with("my-app"))
        self.assertEqual(out, [])

    def test_an_unblocked_card_with_a_stale_decision_summary_is_not_listed(self):
        kb = self._runner("ready", [], latest_summary="DECISION: answered days ago")
        out = dc.blocked_decisions(kanban=kb, boards_dir=boards_with("my-app"))
        self.assertEqual(out, [])

    def test_a_decision_block_is_listed_whichever_path_issued_it(self):
        # C2: a worker's DECISION comes through `kanban_block` and writes no
        # comment. Reading the trail alone found nothing, so no decision card
        # was ever detected and the owner never got the 🧭 message.
        for label, comments, events in block_shapes(BLOCK):
            with self.subTest(shape=label):
                kb = self._runner("blocked", comments, events=events,
                                  latest_summary="something the board wrote later")
                out = dc.blocked_decisions(kanban=kb, boards_dir=boards_with("my-app"))
                self.assertEqual(len(out), 1)
                self.assertTrue(out[0]["reason"].startswith(dc.DECISION_PREFIX))
                self.assertEqual(out[0]["task_id"], "t_1")


    def test_a_decision_block_in_triage_is_listed(self):
        # Live bug (2026-09-11): a review card that raised a second DECISION
        # got parked in `triage` by the board's block-loop detector
        # (kanban_db.py:~6406) instead of `blocked`, and the DECISION was
        # invisible to this scan. `b.list(statuses=...)` must include
        # `triage`, and `current_block_reason` already reads triage's
        # `block_loop_detected` event the same way it reads a blocked card's
        # `blocked` event (board.block_events, BLOCK_EVENT_KINDS).
        for label, comments, events in block_shapes(BLOCK):
            with self.subTest(shape=label):
                kb = self._runner("triage", comments, events=events,
                                  latest_summary="something the board wrote later")
                out = dc.blocked_decisions(kanban=kb, boards_dir=boards_with("my-app"))
                self.assertEqual(len(out), 1)
                self.assertTrue(out[0]["reason"].startswith(dc.DECISION_PREFIX))
                self.assertEqual(out[0]["task_id"], "t_1")

    def test_a_plain_triage_intake_card_is_not_listed(self):
        # An ordinary un-specified triage card (no block record at all) must
        # not be mistaken for an open DECISION just because it now shares a
        # status with a loop-parked one.
        kb = self._runner("triage", [], events=())
        out = dc.blocked_decisions(kanban=kb, boards_dir=boards_with("my-app"))
        self.assertEqual(out, [])

    def test_an_answered_and_unblocked_decision_is_not_relisted_after_a_later_repair_hold(self):
        # NEW-1 repro: the foreign
        # DECISION event is retired by the `unblocked` event that lifted it,
        # so a later, unrelated repair hold must not be reported to the owner
        # as still an open decision.
        events = [{"kind": "blocked", "created_at": 1000,
                  "payload": {"reason": "DECISION: which cache?"}},
                 {"kind": "unblocked", "created_at": 1100, "payload": None}]
        comments = [{"author": "bridge", "body": "BLOCKED: repair: retry 1/3 at 10:00 (crashed)",
                    "created_at": 6000}]
        kb = self._runner("blocked", comments, events=events)
        out = dc.blocked_decisions(kanban=kb, boards_dir=boards_with("my-app"))
        self.assertEqual(out, [])


class LegacyModesRemoved(unittest.TestCase):
    def test_run_and_guard_are_gone_and_answer_is_the_only_mode(self):
        gone = ("run", "guard", "open_one", "phone_message", "GUARD_MESSAGE", "_read_stdin_payload")
        for name in gone:
            self.assertFalse(hasattr(dc, name), name)
        for mode in ("run", "guard"):
            with unittest.mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                dc.main([mode])


class BoardSeam(unittest.TestCase):
    def test_kanban_calls_pin_the_board_they_name(self):
        seen = []
        def seam(argv, env=None):
            seen.append((argv, env))
            return ""
        dc._board("my-app", kanban=seam).comment("t_1", "x")
        argv, env = seen[0]
        self.assertEqual(argv[:5], ["-m", "legacy_cli.main", "kanban", "--board", "my-app"])
        self.assertTrue(env["LEGACY_KANBAN_DB"].endswith("/boards/my-app/kanban.db"))
        self.assertNotIn("LEGACY_KANBAN_TASK", env)

    def test_board_re_and_the_repair_prefix_are_boards_definitions(self):
        import board
        self.assertIs(dc.BOARD_RE, board.BOARD_RE)


class ClassField(unittest.TestCase):
    BLOCK = ("DECISION: Split this card?\nWhy: it is 12 files\nClass: R2\n"
             "1. split - two cards\n2. drop - lose it\n"
             "Recommend: 1 - smaller cards merge\nLearn: size is a design signal")

    def test_the_class_is_parsed(self):
        self.assertEqual(dc.parse_decision(self.BLOCK)["class"], "R2")

    def test_a_block_without_one_defaults_to_r3(self):
        self.assertEqual(dc.parse_decision(self.BLOCK.replace("Class: R2\n", ""))["class"], "R3")

    def test_a_bad_class_falls_back_to_r3_instead_of_raising(self):
        # Final review C1: the class is a hint, not a gate - an otherwise
        # well-formed DECISION must never be swallowed by this field.
        self.assertEqual(dc.parse_decision(self.BLOCK.replace("R2", "urgent"))["class"], "R3")

    def test_the_glossary_parenthetical_still_parses(self):
        block = self.BLOCK.replace("Class: R2", "Class: R2 (costly to undo)")
        self.assertEqual(dc.parse_decision(block)["class"], "R2")
        block = self.BLOCK.replace("Class: R2", "Class: R2 - costly to undo")
        self.assertEqual(dc.parse_decision(block)["class"], "R2")

    def test_the_class_survives_the_note_round_trip(self):
        d = dc.parse_decision(self.BLOCK)
        note = dc.render_note(d, board="my-app", card="t_1", raised_by="dev",
                              repo="my-app", date="2026-09-10", title="Split")
        self.assertIn("class: R2", note)

