import os, sys, tempfile, unittest, unittest.mock
import importlib.util
from pathlib import Path
sys.path[:0] = [str(Path(__file__).resolve().parents[n]) for n in (1, 2)]
import capsule.tests  # noqa: E402,F401 - its __init__ installs the swept TMPDIR (tmpclean)
os.environ["AGENT_OPS_NO_SEND"] = "1"
os.environ.setdefault("EVENTS_SPOOL", str(Path(tempfile.mkdtemp(prefix="events-intake-")) / "events.jsonl"))
import intake
sys.path.insert(0, str(Path(__file__).resolve().parent))
import board, merge_gate               # noqa: E402
import board_view                      # noqa: E402
_spec = importlib.util.spec_from_file_location("fakes", Path(__file__).with_name("fakes.py"))
fakes = sys.modules["fakes"] = importlib.util.module_from_spec(_spec)   # not `import fakes`: rule 1
_spec.loader.exec_module(fakes)

BOARDS = ("capsule-corp", "my-app")

SAMPLE = """---
type: intake
---
<!-- Your file. One instruction per line. -->
## New work
- repo: my-app | risk: med | Add a weekly summary email of finished sessions
- repo: capsule-corp | Investigate why the nightly report is late
- repo: potato | Add a thing
## Approvals
- approve spec: retention-sweep-first-cycle-prune-cli
- approve pr: my-app t_9dc82d7b
- drop: my-app t_04ad2000
## Notes
- repo: my-app | this is a note and must never run
"""


class ParseLine(unittest.TestCase):
    def p(self, line):
        return intake.parse_line(line, boards=BOARDS)

    def test_new_work_with_risk(self):
        got = self.p("- repo: my-app | risk: high | Add a weekly summary email")
        self.assertEqual(got["verb"], "new")
        self.assertEqual(got["repo"], "my-app")
        self.assertEqual(got["risk"], "high")
        self.assertEqual(got["title"], "Add a weekly summary email")

    def test_risk_defaults_to_med(self):
        self.assertEqual(self.p("- repo: capsule-corp | Investigate the late report")["risk"], "med")

    def test_a_title_may_contain_pipes_after_the_first_two_fields(self):
        got = self.p("- repo: my-app | risk: low | Rename a | b | c")
        self.assertEqual(got["title"], "Rename a | b | c")

    def test_unknown_repo_is_an_error_naming_the_repo(self):
        got = self.p("- repo: potato | Add a thing")
        self.assertEqual(got["verb"], "error")
        self.assertIn("potato", got["why"])

    def test_unknown_risk_is_an_error(self):
        self.assertEqual(self.p("- repo: my-app | risk: spicy | x")["verb"], "error")

    def test_empty_title_is_an_error(self):
        self.assertEqual(self.p("- repo: my-app | risk: low |   ")["verb"], "error")

    def test_approve_spec(self):
        got = self.p("- approve spec: retention-sweep")
        self.assertEqual((got["verb"], got["slug"]), ("approve_spec", "retention-sweep"))

    def test_a_path_traversing_slug_is_an_error(self):
        self.assertEqual(self.p("- approve spec: ../../etc/passwd")["verb"], "error")

    def test_the_grammar_itself_only_admits_the_slug_charset(self):
        r"""C0 item 6 / threat model 4.2: the capture was `\S+`. Now the line
        does not parse as an approval at all unless the slug is slug-shaped -
        and every non-slug shape still ends as an `error`, never a verb."""
        import intake
        for bad in ("../../etc/passwd", "Specs/x.md", "UPPER", "a b", "a/b", "a.b", "a_b", "-lead", ""):
            self.assertIsNone(intake._APPROVE_SPEC_RE.match(f"- approve spec: {bad}"), bad)
            self.assertEqual(self.p(f"- approve spec: {bad}")["verb"], "error", bad)
        for good in ("retention-sweep", "a", "soft-tick-toggle", "a1-b2"):
            self.assertIsNotNone(intake._APPROVE_SPEC_RE.match(f"- approve spec: {good}"), good)
            self.assertEqual(self.p(f"- approve spec: {good}")["verb"], "approve_spec", good)

    def test_approve_pr_is_an_ordinary_malformed_approval(self):
        """2.2: the reserved verb is gone. `approve pr:` is now just an
        approval line the grammar does not know, same as `approve merge:` -
        there is no special-cased PR verb left to be mistaken for a supported
        one, and no code path that could ever grow into honouring it."""
        for line in ("- approve pr: my-app t_9dc82d7b",
                     "- approve pr: my-app 04ad2000",
                     "- approve pr: my-app t_ZZZZ",
                     "- approve   pr:  my-app   t_9dc82d7b",
                     "- approve merge: my-app t_04ad2000",
                     "- approve everything"):
            got = self.p(line)
            self.assertEqual(got["verb"], "error", line)
            self.assertEqual(got["why"], intake.UNKNOWN_APPROVAL, line)

    def test_the_unknown_approval_error_names_the_github_merge(self):
        self.assertIn("GitHub merge", intake.UNKNOWN_APPROVAL)

    def test_the_reserved_verb_is_gone_from_the_module(self):
        self.assertFalse(hasattr(intake, "APPROVE_PR_REFUSED"))
        self.assertFalse(hasattr(intake, "_APPROVE_PR_RE"))

    def test_a_real_spec_approval_still_parses(self):
        self.assertEqual(self.p("- approve spec: soft-tick-toggle")["verb"], "approve_spec")

    def test_a_bad_task_id_is_an_error(self):
        self.assertEqual(self.p("- drop: my-app 04ad2000")["verb"], "error")
        self.assertEqual(self.p("- drop: my-app t_ZZZZ")["verb"], "error")

    def test_drop(self):
        g = self.p("- drop: my-app t_04ad2000")
        self.assertEqual((g["verb"], g["board"], g["task_id"]), ("drop", "my-app", "t_04ad2000"))

    def test_prose_is_an_error_not_a_crash(self):
        self.assertEqual(self.p("- remind me to call the bank")["verb"], "error")

    def test_a_trailing_error_comment_is_stripped_before_matching(self):
        line = "- repo: my-app | Add a thing   <!-- error: no board named 'potato' (2026-09-07) -->"
        self.assertEqual(intake.strip_error(line).rstrip(), "- repo: my-app | Add a thing")
        self.assertEqual(self.p(line)["verb"], "new")

    def test_the_key_ignores_a_trailing_error_comment_and_whitespace(self):
        a = intake.line_key("- repo: my-app | Add a thing")
        b = intake.line_key("  - repo: my-app | Add a thing  <!-- error: x (2026-09-07 14:20) -->")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 16)
        self.assertNotEqual(a, intake.line_key("- repo: my-app | Add another thing"))

    def test_a_risk_clause_with_no_title_is_an_error(self):
        got = self.p("- repo: my-app | risk: med")
        self.assertEqual(got["verb"], "error")
        self.assertIn("title", got["why"])


class Parse(unittest.TestCase):
    def test_only_the_two_consumable_sections_are_parsed(self):
        got = intake.parse(SAMPLE, boards=BOARDS)
        self.assertEqual(len(got), 6)
        self.assertNotIn("never run", " ".join(e["raw"] for e in got))

    def test_entries_carry_their_section_and_file_order(self):
        got = intake.parse(SAMPLE, boards=BOARDS)
        self.assertEqual([e["verb"] for e in got],
                         ["new", "new", "error", "approve_spec", "error", "drop"])
        self.assertEqual(got[0]["section"], "## New work")
        self.assertEqual(got[3]["section"], "## Approvals")
        self.assertEqual([e["index"] for e in got], sorted(e["index"] for e in got))

    def test_an_unknown_heading_is_treated_as_notes(self):
        text = "## Random thoughts\n- repo: my-app | x\n"
        self.assertEqual(intake.parse(text, boards=BOARDS), [])

    def test_a_rejected_section_is_not_consumable(self):
        text = "## Rejected\n- repo: my-app | x\n"
        self.assertEqual(intake.parse(text, boards=BOARDS), [])


class Render(unittest.TestCase):
    def _rendered(self, **kw):
        args = dict(consumed=[], errors={}, receipts=[], rejected=[], now="2026-09-07 14:20")
        args.update(kw)
        return intake.render(SAMPLE, **args)

    def test_consumed_lines_are_removed_and_headings_kept(self):
        entries = intake.parse(SAMPLE, boards=BOARDS)
        out = self._rendered(consumed=[e["index"] for e in entries if e["verb"] != "error"])
        self.assertNotIn("Add a weekly summary email", out)
        self.assertNotIn("approve spec:", out)
        self.assertIn("## New work", out)
        self.assertIn("## Approvals", out)

    def test_notes_are_byte_identical(self):
        out = self._rendered(consumed=[0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
        notes = lambda t: t.split("## Notes", 1)[1]
        self.assertEqual(notes(out), notes(SAMPLE))

    def test_the_frontmatter_and_header_comment_survive(self):
        out = self._rendered()
        self.assertTrue(out.startswith("---\ntype: intake\n---\n"))
        self.assertIn("<!-- Your file.", out)

    def test_an_error_line_stays_with_exactly_one_comment(self):
        entries = intake.parse(SAMPLE, boards=BOARDS)
        bad = next(e for e in entries if e["verb"] == "error")
        out = self._rendered(errors={bad["index"]: "no board named 'potato'"})
        line = next(l for l in out.splitlines() if "potato" in l)
        self.assertEqual(line.count("<!-- error:"), 1)
        self.assertIn("no board named 'potato' (2026-09-07 14:20)", line)

    def test_re_erroring_replaces_the_comment_never_appends(self):
        entries = intake.parse(SAMPLE, boards=BOARDS)
        bad = next(e for e in entries if e["verb"] == "error")
        once = self._rendered(errors={bad["index"]: "no board named 'potato'"})
        twice = intake.render(once, consumed=[], errors={
            next(e["index"] for e in intake.parse(once, boards=BOARDS) if e["verb"] == "error"):
                "no board named 'potato'"}, receipts=[], rejected=[], now="2026-09-07 14:30")
        line = next(l for l in twice.splitlines() if "potato" in l)
        self.assertEqual(line.count("<!-- error:"), 1)
        self.assertIn("14:30", line)

    def test_receipts_appear_under_processed_newest_first_and_capped(self):
        out = self._rendered(receipts=[f"- 2026-09-07 14:20 · created my-app/t_{i:08x} · Card {i}"
                                      for i in range(25)])
        block = out.split(intake.PROCESSED, 1)[1]
        self.assertEqual(len([l for l in block.splitlines() if l.startswith("- ")]), intake.RECEIPT_CAP)

    def test_a_line_that_erred_five_ticks_moves_under_rejected(self):
        entries = intake.parse(SAMPLE, boards=BOARDS)
        bad = next(e for e in entries if e["verb"] == "error")
        out = self._rendered(rejected=[bad["index"]])
        self.assertIn(intake.REJECTED, out)
        rejected_block = out.split(intake.REJECTED, 1)[1]
        self.assertIn("potato", rejected_block)
        self.assertNotIn("potato", out.split(intake.REJECTED, 1)[0])


class Consume(unittest.TestCase):
    def setUp(self):
        self.boards = Path(tempfile.mkdtemp(prefix="in-boards-"))
        for n in ("my-app", "capsule-corp"):
            (self.boards / n).mkdir(parents=True)
            (self.boards / n / "kanban.db").write_text("")
        self.h = fakes.FakeLegacyCli(self.boards)
        self.drop_id = self.h.board("my-app").create(board.NewCard(title="Old card"))
        self.vault = Path(tempfile.mkdtemp(prefix="in-vault-"))
        (self.vault / "Boards").mkdir()
        self.state = Path(tempfile.mkdtemp(prefix="in-state-"))
        self._old_state = os.environ.get("MERGE_GATE_STATE")
        os.environ["MERGE_GATE_STATE"] = str(self.state)
        self.seen = Path(tempfile.mkdtemp(prefix="in-seen-")) / "notify-seen.json"
        self.sent = []
        self.path = self.vault / "Boards" / "Intake.md"
        self.path.write_text(f"""---
type: intake
---
## New work
- repo: my-app | risk: med | Add a weekly summary email
- repo: potato | Add a thing
## Approvals
- approve spec: retention-sweep
- approve pr: my-app t_9dc82d7b
- drop: my-app {self.drop_id}
## Findings
- repo: my-app | risk: high | Pin the npm install (security 2026-09-08)
## Notes
- repo: my-app | never run this
""", encoding="utf-8")

    def tearDown(self):
        if self._old_state is None:
            os.environ.pop("MERGE_GATE_STATE", None)
        else:
            os.environ["MERGE_GATE_STATE"] = self._old_state

    def _consume(self):
        return intake.consume(vault=self.vault, boards_dir=self.boards, runner=self.h,
                              sender=lambda m: self.sent.append(m) or True,
                              seen_path=self.seen, state_dir=self.state,
                              git_runner=lambda cmd: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
                              now="2026-09-07 14:20")

    def test_the_three_good_lines_run_and_are_cleared(self):
        res = self._consume()
        self.assertEqual(len(res["consumed"]), 3)
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("- repo: my-app | risk: med | Add a weekly summary email", text)
        self.assertNotIn("- approve spec: retention-sweep", text)
        self.assertNotIn(f"- drop: my-app {self.drop_id}", text)
        # Refused, so it stays put carrying its reason.
        self.assertIn("- approve pr: my-app t_9dc82d7b", text)

    def test_a_card_is_created_in_triage_carrying_its_source_key(self):
        self._consume()
        card = next(c for c in self.h.tasks.values() if c["title"] == "Add a weekly summary email")
        self.assertIn("source: intake:", card["body"])
        self.assertIn("risk: med", card["body"])

    def test_the_spec_stamp_is_written(self):
        self._consume()
        self.assertIsNotNone(merge_gate.read_spec_approval("retention-sweep"))
        self.assertEqual(merge_gate.read_spec_approval("retention-sweep")["via"], "intake")

    def test_no_pr_stamp_is_ever_written_from_intake(self):
        """An `approve pr:` line must leave the gate's approval dir untouched
        and stay in the file carrying the unknown-approval reason."""
        self._consume()
        self.assertFalse((merge_gate.state_dir() / "approvals/my-app/t_9dc82d7b.json").exists())
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("- approve pr: my-app t_9dc82d7b", text)
        self.assertIn(intake.UNKNOWN_APPROVAL, text)

    def test_drop_archives_the_card(self):
        self._consume()
        self.assertEqual(self.h.tasks[self.drop_id]["status"], "archived")

    def test_the_bad_line_stays_with_exactly_one_error_comment(self):
        self._consume()
        line = next(l for l in self.path.read_text(encoding="utf-8").splitlines() if "potato" in l)
        self.assertEqual(line.count("<!-- error:"), 1)
        self.assertIn("no board named 'potato'", line)

    def _notes_body(self, text):
        # `## Processed`/`## Rejected` are appended at the true end of the
        # file, after `## Notes` (intake.py's `_replace_section`, deliberate:
        # see the module docstring on _replace_section) - so "everything
        # after ## Notes" is no longer the right byte-for-byte check once a
        # tick has run. Only the Notes heading's own body is a promise.
        after = text.split("## Notes", 1)[1].splitlines()
        end = next((i for i, l in enumerate(after) if l.startswith("## ")), len(after))
        return "\n".join(after[:end])

    def test_notes_survive_byte_for_byte(self):
        before = self._notes_body(self.path.read_text(encoding="utf-8"))
        self._consume()
        self.assertEqual(self._notes_body(self.path.read_text(encoding="utf-8")), before)

    def test_receipts_name_what_happened(self):
        self._consume()
        block = self.path.read_text(encoding="utf-8").split(intake.PROCESSED, 1)[1]
        self.assertIn("created my-app/", block)
        self.assertIn("Add a weekly summary email", block)

    def test_consuming_again_creates_nothing_new(self):
        self._consume()
        n_tasks = len(self.h.tasks)
        stamp_ts = merge_gate.read_spec_approval("retention-sweep")["ts"]
        res = self._consume()
        self.assertEqual(len(self.h.tasks), n_tasks)
        self.assertEqual(res["consumed"], [])
        self.assertEqual(merge_gate.read_spec_approval("retention-sweep")["ts"], stamp_ts)
        line = next(l for l in self.path.read_text(encoding="utf-8").splitlines() if "potato" in l)
        self.assertEqual(line.count("<!-- error:"), 1)

    def test_the_same_line_twice_in_one_file_makes_one_card(self):
        self.path.write_text("---\ntype: intake\n---\n## New work\n"
                             "- repo: my-app | risk: low | Twin\n"
                             "- repo: my-app | risk: low | Twin\n", encoding="utf-8")
        self._consume()
        self.assertEqual(sum(1 for t in self.h.tasks.values() if t["title"] == "Twin"), 1)

    def test_an_error_five_ticks_running_moves_under_rejected(self):
        for _ in range(intake.ERROR_TICKS):
            self._consume()
        text = self.path.read_text(encoding="utf-8")
        self.assertIn(intake.REJECTED, text)
        self.assertIn("potato", text.split(intake.REJECTED, 1)[1])

    def test_one_phone_message_per_bad_line_ever(self):
        self._consume()
        self._consume()
        self.assertEqual(sum(1 for m in self.sent if "potato" in m), 1)

    def test_a_missing_file_is_created_from_the_template(self):
        pushed = []
        self.path.unlink()
        res = intake.consume(vault=self.vault, boards_dir=self.boards, runner=self.h,
                             sender=lambda m: True, seen_path=self.seen, state_dir=self.state,
                             git_runner=lambda cmd: (pushed.append(list(cmd)) or
                                                     type("R", (), {"returncode": 1 if "diff" in cmd else 0,
                                                                    "stdout": "", "stderr": ""})()),
                             now="2026-09-07 14:20")
        self.assertEqual(res["consumed"], [])
        self.assertIn("## New work", self.path.read_text(encoding="utf-8"))
        self.assertIn("## Notes", self.path.read_text(encoding="utf-8"))
        # I5: created only on this host is useless - the phone is where the
        # file is meant to be written.
        add = next((" ".join(c) for c in pushed if " add " in " ".join(c)), None)
        self.assertIsNotNone(add, f"no git add in {pushed}")
        self.assertIn("Boards/Intake.md", add)
        self.assertTrue(any("intake: create" in " ".join(c) for c in pushed), pushed)

    def test_it_pushes_only_intake(self):
        pushed = []

        def _git(cmd):
            pushed.append(list(cmd))
            # real git semantics: `diff --cached --quiet` exits 1 when there is
            # something staged, 0 when the index is clean - so this mock must
            # say "staged" or vault_git.push short-circuits before `commit`
            # (the same pattern test_board_view.Migrate.test_it_pushes_both_files_in_one_commit uses).
            rc = 1 if "diff" in cmd else 0
            return type("R", (), {"returncode": rc, "stdout": "", "stderr": ""})()

        intake.consume(vault=self.vault, boards_dir=self.boards, runner=self.h,
                       sender=lambda m: True, seen_path=self.seen, state_dir=self.state,
                       git_runner=_git, now="2026-09-07 14:20")
        add = next(" ".join(c) for c in pushed if " add " in " ".join(c))
        self.assertIn("Boards/Intake.md", add)
        # I6: the generated board is board_view.regenerate's to push - it runs
        # later in the same tick and short-circuits when nothing moved.
        self.assertNotIn("Boards/Engineering.md", add)
        self.assertTrue(any("intake: 3 lines" in " ".join(c) for c in pushed))

    def test_a_findings_line_is_never_consumed(self):
        self._consume()
        self.assertIn("- repo: my-app | risk: high | Pin the npm install (security 2026-09-08)",
                      self.path.read_text(encoding="utf-8"))
        self.assertFalse(any(c["title"].startswith("Pin the npm install")
                             for c in self.h.tasks.values()))

    def test_a_findings_line_moved_under_new_work_is_consumed(self):
        line = "- repo: my-app | risk: high | Pin the npm install (security 2026-09-08)\n"
        text = self.path.read_text(encoding="utf-8")
        self.path.write_text(text.replace(f"{intake.NEW_WORK}\n", f"{intake.NEW_WORK}\n{line}"),
                             encoding="utf-8")
        self._consume()
        self.assertTrue(any(c["title"] == "Pin the npm install (security 2026-09-08)"
                            for c in self.h.tasks.values()))
        # the copy still under ## Findings is untouched: `parse` never returned it
        self.assertIn(line, self.path.read_text(encoding="utf-8"))


class ConsumeGuards(unittest.TestCase):
    def setUp(self):
        Consume.setUp(self)

    def tearDown(self):
        Consume.tearDown(self)

    def test_consume_refuses_the_default_vault_under_test(self):
        """I2: the same guard board_view.write carries. AGENT_OPS_NO_SEND is
        the suite's marker - under it an implicit vault is always a bug."""
        self.assertTrue(os.environ.get("AGENT_OPS_NO_SEND"))
        with self.assertRaises(RuntimeError):
            intake.consume(boards_dir=self.boards, runner=self.h, seen_path=self.seen,
                           state_dir=self.state)

    def test_dropping_an_already_archived_card_is_success(self):
        """M4: a second tick, or a card dropped from the phone first, must not
        leave an error comment sitting on a line that got what it asked for."""
        self.h.tasks[self.drop_id]["status"] = "archived"
        res = Consume._consume(self)
        self.assertNotIn("archive", " ".join(res["errors"].values()))
        self.assertTrue(any("already dropped" in r for r in res["receipts"]), res["receipts"])
        self.assertNotIn(f"- drop: my-app {self.drop_id}",
                         self.path.read_text(encoding="utf-8"))

    def test_a_held_lock_leaves_the_file_untouched(self):
        before = self.path.read_bytes()
        b = board.open_board("my-app", runner=self.h, boards_dir=self.boards)
        with b.lock(intake.INTAKE_LOCK_TASK, state_dir=self.state):
            # the pseudo-lock is (INTAKE_LOCK_BOARD, INTAKE_LOCK_TASK); take it
            # on the same path consume will try
            pass
        import fcntl
        p = board.lock_path(intake.INTAKE_LOCK_BOARD, intake.INTAKE_LOCK_TASK, self.state)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            res = Consume._consume(self)
        self.assertEqual(res["skipped"], "locked")
        self.assertEqual(self.path.read_bytes(), before)

    def test_a_failing_pull_leaves_the_file_untouched(self):
        before = self.path.read_bytes()
        def failing(cmd):
            rc = 1 if "pull" in cmd else 0
            return type("R", (), {"returncode": rc, "stdout": "", "stderr": "diverged"})()
        res = intake.consume(vault=self.vault, boards_dir=self.boards, runner=self.h,
                             sender=lambda m: True, seen_path=self.seen, state_dir=self.state,
                             git_runner=failing, now="2026-09-07 14:20")
        self.assertEqual(res["skipped"], "pull")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.h.tasks[self.drop_id]["status"], "ready")

    def test_an_edit_during_the_tick_abandons_the_write_but_keeps_the_effects(self):
        original = self.path
        real_read = Path.read_text
        state = {"n": 0}

        def racing(self_path, *a, **kw):
            out = real_read(self_path, *a, **kw)
            if self_path == original:
                state["n"] += 1
                if state["n"] == 2:      # the re-read just before writing back
                    return out + "- repo: my-app | risk: low | Typed mid-tick\n"
            return out

        with unittest.mock.patch.object(Path, "read_text", racing):
            res = Consume._consume(self)
        self.assertFalse(res["written"])
        self.assertIn("Add a weekly summary email", self.path.read_text(encoding="utf-8"))
        self.assertTrue(any(t["title"] == "Add a weekly summary email" for t in self.h.tasks.values()))
        # next tick, no race: the effects are keyed, so nothing doubles
        res2 = Consume._consume(self)
        self.assertTrue(res2["written"])
        self.assertEqual(sum(1 for t in self.h.tasks.values()
                             if t["title"] == "Add a weekly summary email"), 1)

    def test_no_vault_is_a_silent_no_op(self):
        res = intake.consume(vault=self.vault / "gone", boards_dir=self.boards, runner=self.h,
                             sender=lambda m: True, seen_path=self.seen, state_dir=self.state,
                             git_runner=lambda cmd: type("R", (), {"returncode": 0})(),
                             now="2026-09-07 14:20")
        self.assertEqual(res["skipped"], "no-vault")


class LedgerEntries(unittest.TestCase):
    """intake's two ledgered verbs: `new` -> created, `drop` -> abandoned."""

    def setUp(self):
        import ledger
        self.ledger = ledger
        self._old_vault = os.environ.get("OBSIDIAN_VAULT_PATH")
        self._old_state = os.environ.get("MERGE_GATE_STATE")
        self.vault = Path(tempfile.mkdtemp(prefix="intake-ledger-"))
        (self.vault / "Boards").mkdir()
        os.environ["OBSIDIAN_VAULT_PATH"] = str(self.vault)
        os.environ["MERGE_GATE_STATE"] = tempfile.mkdtemp(prefix="intake-ledger-state-")
        self.boards = Path(tempfile.mkdtemp(prefix="intake-ledger-boards-"))
        (self.boards / "my-app").mkdir()
        (self.boards / "my-app" / "kanban.db").write_text("")
        self.h = fakes.FakeLegacyCli(self.boards)

    def tearDown(self):
        for name, old in (("OBSIDIAN_VAULT_PATH", self._old_vault),
                          ("MERGE_GATE_STATE", self._old_state)):
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old

    def test_a_new_line_writes_a_created_entry(self):
        entry = intake.parse("## New work\n- repo: my-app | risk: low | Soft tick\n",
                             boards=("my-app",))[0]
        receipt, reason = intake._execute(entry, boards_dir=self.boards, runner=self.h,
                                          now="2026-09-08 10:00")
        self.assertIsNone(reason)
        tid = self.h.created[-1]["id"]
        rows = self.ledger.read("my-app", tid)
        self.assertEqual([r["kind"] for r in rows], ["created"])
        self.assertEqual(rows[0]["source"], "intake")
        self.assertEqual(rows[0]["payload"]["risk"], "low")

    def test_a_drop_line_writes_an_abandoned_entry(self):
        tid = self.h.board("my-app").create(board.NewCard(title="X"))
        entry = intake.parse(f"## Approvals\n- drop: my-app {tid}\n",
                             boards=("my-app",))[0]
        receipt, reason = intake._execute(entry, boards_dir=self.boards, runner=self.h,
                                          now="2026-09-08 10:00")
        self.assertIsNone(reason)
        self.assertEqual([r["kind"] for r in self.ledger.read("my-app", tid)], ["abandoned"])


class FindingsSection(unittest.TestCase):
    """Row 87: `## Findings` is written by the nightly security job and the
    monthly agent review, and consumed by nobody. It must be in the template
    between Approvals and Notes, must never be parsed, and must survive a
    render byte-for-byte - a finding becomes a card only when the owner moves
    the line under `## New work`."""

    def test_the_template_carries_findings_between_approvals_and_notes(self):
        t = board_view.INTAKE_TEMPLATE
        self.assertIn(intake.FINDINGS, t)
        self.assertLess(t.index(intake.APPROVALS), t.index(intake.FINDINGS))
        self.assertLess(t.index(intake.FINDINGS), t.index(intake.NOTES))

    def test_the_template_says_nothing_consumes_it(self):
        self.assertIn("## New work", board_view.INTAKE_TEMPLATE.split(intake.FINDINGS)[0][-400:])

    def test_findings_is_not_consumable(self):
        self.assertNotIn(intake.FINDINGS, intake.CONSUMABLE)

    def test_no_findings_line_is_ever_parsed(self):
        text = (f"{intake.FINDINGS}\n"
                "- repo: my-app | risk: high | Pin the npm install (security 2026-09-08)\n"
                "- repo: capsule-corp | risk: med | Quote the board title (security 2026-09-08)\n")
        self.assertEqual(intake.parse(text, boards=BOARDS), [])

    def test_a_findings_block_survives_a_render_byte_for_byte(self):
        block = (f"{intake.FINDINGS}\n"
                 "- repo: my-app | risk: high | Pin the npm install (security 2026-09-08)\n"
                 "- repo: capsule-corp | risk: med | Quote the board title (security 2026-09-08)\n"
                 "- repo: capsule-corp | risk: med | Cap the huddle thread (agent review 2026-09)\n")
        text = f"---\ntype: intake\n---\n{intake.NEW_WORK}\n{block}{intake.NOTES}\n"
        out = intake.render(text, consumed=[], errors={}, receipts=[], rejected=[],
                            now="2026-09-08")
        self.assertIn(block, out)


class AddFindingCli(unittest.TestCase):
    """Final review C1: the nightly `security` and monthly `agent-review`
    sessions read untrusted code and prose, so they get NO Edit on
    Boards/Intake.md - only this verb, which can write one validated line in
    one section and nothing else. Anything it would refuse must leave the file
    byte-for-byte as it was."""

    OTHER = (f"{intake.NEW_WORK}\n"
             "- repo: my-app | risk: med | Add a weekly summary email\n"
             f"{intake.APPROVALS}\n"
             "- approve spec: retention-sweep\n")

    def setUp(self):
        self.boards = Path(tempfile.mkdtemp(prefix="af-boards-"))
        for n in ("my-app", "capsule-corp"):
            (self.boards / n).mkdir(parents=True)
            (self.boards / n / "kanban.db").write_text("")
        self.vault = Path(tempfile.mkdtemp(prefix="af-vault-"))
        (self.vault / "Boards").mkdir()
        self.path = self.vault / "Boards" / "Intake.md"
        self._write(f"---\ntype: intake\n---\n{self.OTHER}"
                    f"{intake.FINDINGS}\n"
                    "- repo: my-app | risk: high | Pin the npm install (security 2026-09-07)\n"
                    f"{intake.NOTES}\n- a note that must never move\n")

    def _write(self, text):
        self.path.write_text(text, encoding="utf-8")

    def _add(self, *args, source="security 2026-09-08"):
        return intake.add_finding(*args, source, vault=self.vault, boards_dir=self.boards)

    def _cli(self, argv):
        import io, contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = intake.main(argv)
        return rc, err.getvalue()

    def test_a_finding_is_appended_at_the_end_of_the_findings_section(self):
        _, appended = self._add("capsule-corp", "med", "Quote the board title")
        self.assertTrue(appended)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        i = lines.index(intake.FINDINGS)
        self.assertEqual(lines[i + 1:i + 3],
                         ["- repo: my-app | risk: high | Pin the npm install (security 2026-09-07)",
                          "- repo: capsule-corp | risk: med | Quote the board title (security 2026-09-08)"])
        self.assertEqual(lines[i + 3], intake.NOTES)

    def test_the_appended_line_parses_as_the_new_verb_when_moved(self):
        """The whole point of the shape: the owner moves the line up under
        `## New work` and the bridge makes a card, nothing retyped."""
        self._add("capsule-corp", "med", "Quote the board title")
        line = [l for l in self.path.read_text(encoding="utf-8").splitlines()
                if "Quote the board title" in l][0]
        got = intake.parse_line(line, boards=("capsule-corp", "my-app"))
        self.assertEqual(got["verb"], "new")
        self.assertEqual(got["repo"], "capsule-corp")
        self.assertEqual(got["risk"], "med")
        self.assertEqual(got["title"], "Quote the board title (security 2026-09-08)")

    def test_the_heading_is_created_immediately_before_notes(self):
        self._write(f"---\ntype: intake\n---\n{self.OTHER}{intake.NOTES}\n- keep me\n")
        self._add("capsule-corp", "low", "Add a heading")
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[-4:], [intake.FINDINGS,
                                      "- repo: capsule-corp | risk: low | Add a heading (security 2026-09-08)",
                                      intake.NOTES, "- keep me"])

    def test_the_heading_goes_at_the_end_of_the_file_when_there_is_no_notes(self):
        self._write(f"---\ntype: intake\n---\n{self.OTHER}")
        self._add("capsule-corp", "low", "Add a heading")
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[-2:], [intake.FINDINGS,
                                      "- repo: capsule-corp | risk: low | Add a heading (security 2026-09-08)"])

    def test_an_identical_second_call_appends_nothing(self):
        self._add("capsule-corp", "med", "Quote the board title")
        after_first = self.path.read_bytes()
        _, appended = self._add("capsule-corp", "med", "Quote the board title")
        self.assertFalse(appended)
        self.assertEqual(self.path.read_bytes(), after_first)
        rc, _ = self._cli(["add-finding", "capsule-corp", "med", "Quote", "the", "board", "title",
                           "--source", "security 2026-09-08", "--vault", str(self.vault),
                           "--boards-dir", str(self.boards)])
        self.assertEqual(rc, 0)
        self.assertEqual(self.path.read_bytes(), after_first)

    def test_no_other_section_is_touched(self):
        before = self.path.read_text(encoding="utf-8")
        self._add("capsule-corp", "med", "Quote the board title")
        after = self.path.read_text(encoding="utf-8")
        for chunk in (f"---\ntype: intake\n---\n{self.OTHER}",
                      f"{intake.NOTES}\n- a note that must never move\n"):
            self.assertIn(chunk, before)
            self.assertIn(chunk, after)

    def test_a_refused_call_writes_nothing(self):
        for board_name, risk, title in (
                ("potato", "med", "Board does not exist"),
                ("capsule-corp", "critical", "Risk is not in RISKS"),
                ("capsule-corp", "med", "A title with a | pipe"),
                ("capsule-corp", "med", "   "),
                ("capsule-corp", "med", "two\nlines"),
        ):
            before = self.path.read_bytes()
            with self.subTest(board=board_name, risk=risk, title=title):
                with self.assertRaises(ValueError):
                    self._add(board_name, risk, title)
                self.assertEqual(self.path.read_bytes(), before)

    def test_a_source_label_with_a_pipe_is_refused(self):
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self._add("capsule-corp", "med", "Fine title", source="security | 2026-09-08")
        self.assertEqual(self.path.read_bytes(), before)

    def test_the_cli_refuses_with_exit_2_and_one_stderr_line(self):
        before = self.path.read_bytes()
        rc, err = self._cli(["add-finding", "potato", "med", "No", "such", "board",
                             "--source", "security 2026-09-08", "--vault", str(self.vault)])
        self.assertEqual(rc, 2)
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertIn("potato", err)
        self.assertEqual(self.path.read_bytes(), before)

    def test_the_cli_appends_and_exits_0(self):
        rc, err = self._cli(["add-finding", "capsule-corp", "med", "Quote", "the", "board", "title",
                             "--source", "agent review 2026-09", "--vault", str(self.vault),
                             "--boards-dir", str(self.boards)])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("- repo: capsule-corp | risk: med | Quote the board title (agent review 2026-09)",
                      self.path.read_text(encoding="utf-8"))

    def test_the_cli_honours_the_boards_dir_env_var(self):
        """Production callers pass no flag; a test (or a future host that
        wants isolation) can point AGENT_OPS_BOARDS_DIR at a scratch dir
        instead. The flag still wins when both are set."""
        old = os.environ.get("AGENT_OPS_BOARDS_DIR")
        os.environ["AGENT_OPS_BOARDS_DIR"] = str(self.boards)
        try:
            rc, err = self._cli(["add-finding", "capsule-corp", "med", "Quote", "via", "env",
                                 "--source", "agent review 2026-09", "--vault", str(self.vault)])
        finally:
            if old is None:
                os.environ.pop("AGENT_OPS_BOARDS_DIR", None)
            else:
                os.environ["AGENT_OPS_BOARDS_DIR"] = old
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("- repo: capsule-corp | risk: med | Quote via env (agent review 2026-09)",
                      self.path.read_text(encoding="utf-8"))

    def test_a_missing_board_still_exits_2_with_no_board_named(self):
        """The unknown-board error path, proven with a scratch dir that has no such board."""
        before = self.path.read_bytes()
        rc, err = self._cli(["add-finding", "no-such-board", "med", "Missing", "board",
                             "--source", "security 2026-09-08", "--vault", str(self.vault),
                             "--boards-dir", str(self.boards)])
        self.assertEqual(rc, 2)
        self.assertIn("no board named 'no-such-board'", err)
        self.assertEqual(self.path.read_bytes(), before)

    def test_it_refuses_the_default_vault_under_test(self):
        """Same guard consume carries: under AGENT_OPS_NO_SEND an implicit
        default vault would be the real one."""
        self.assertTrue(os.environ.get("AGENT_OPS_NO_SEND"))
        with self.assertRaises(RuntimeError):
            intake.add_finding("capsule-corp", "med", "No vault given", "security 2026-09-08",
                               boards_dir=self.boards)

    def test_it_neither_pulls_nor_pushes(self):
        """The tick's consume already pulls Intake and pushes it; a second
        git writer here is how two writers interleave."""
        calls = []
        with unittest.mock.patch.object(intake.vault_git, "pull",
                                        lambda *a, **k: calls.append("pull") or True), \
             unittest.mock.patch.object(intake.vault_git, "push",
                                        lambda *a, **k: calls.append("push") or True):
            self._add("capsule-corp", "med", "Quote the board title")
        self.assertEqual(calls, [])


class BetFieldOnAnIntakeLine(unittest.TestCase):
    """Row 139 / M249's card-field half for intake-born cards. The flag-off
    tests are the byte-identity fence: with OPS_BET_FIELD unset the `bet:`
    segment is NOT grammar, so it lands in the title exactly as it does today.
    """

    BOARDS = ("my-app", "capsule-corp")

    def setUp(self):
        self._saved = os.environ.pop("OPS_BET_FIELD", None)

    def tearDown(self):
        os.environ.pop("OPS_BET_FIELD", None)
        if self._saved is not None:
            os.environ["OPS_BET_FIELD"] = self._saved

    def _parse(self, line):
        return intake.parse_line(line, boards=self.BOARDS)

    # --- flag off: today's behaviour, exactly -------------------------------
    def test_flag_off_bet_lands_in_the_title(self):
        e = self._parse("- repo: my-app | risk: low | bet: b-1 | Ship it")
        self.assertEqual(e["verb"], "new")
        self.assertEqual(e["risk"], "low")
        self.assertEqual(e["title"], "bet: b-1 | Ship it")
        self.assertNotIn("bet", e)

    def test_flag_off_the_body_has_no_bet_line(self):
        e = self._parse("- repo: my-app | risk: low | bet: b-1 | Ship it")
        e["key"] = "deadbeefdeadbeef"
        self.assertNotIn("bet:", intake._body_for(e).split("-->")[0])

    def test_flag_off_a_plain_line_is_unchanged(self):
        e = self._parse("- repo: my-app | risk: med | Ship it")
        self.assertEqual((e["repo"], e["risk"], e["title"]),
                         ("my-app", "med", "Ship it"))

    # --- flag on ------------------------------------------------------------
    def test_flag_on_bet_is_its_own_field(self):
        os.environ["OPS_BET_FIELD"] = "1"
        e = self._parse("- repo: my-app | risk: low | bet: b-1 | Ship it")
        self.assertEqual((e["repo"], e["risk"], e["bet"], e["title"]),
                         ("my-app", "low", "b-1", "Ship it"))

    def test_flag_on_bet_without_risk_still_parses(self):
        os.environ["OPS_BET_FIELD"] = "1"
        e = self._parse("- repo: my-app | bet: b-1 | Ship it")
        self.assertEqual((e["risk"], e["bet"], e["title"]),
                         (intake.DEFAULT_RISK, "b-1", "Ship it"))

    def test_flag_on_a_line_with_no_bet_carries_none(self):
        os.environ["OPS_BET_FIELD"] = "1"
        e = self._parse("- repo: my-app | risk: low | Ship it")
        self.assertEqual(e["bet"], "")
        self.assertEqual(e["title"], "Ship it")

    def test_flag_on_a_bad_slug_is_an_error_entry(self):
        os.environ["OPS_BET_FIELD"] = "1"
        e = self._parse("- repo: my-app | risk: low | bet: Not A Slug | Ship it")
        self.assertEqual(e["verb"], "error")
        self.assertIn("bad bet slug", e["why"])

    def test_flag_on_bet_with_no_title_is_an_error(self):
        os.environ["OPS_BET_FIELD"] = "1"
        e = self._parse("- repo: my-app | risk: low | bet: b-1 |")
        self.assertEqual(e["verb"], "error")
        self.assertEqual(e["why"], "no title")

    def test_flag_on_the_body_carries_one_bet_line_between_risk_and_source(self):
        os.environ["OPS_BET_FIELD"] = "1"
        e = self._parse("- repo: my-app | risk: low | bet: b-1 | Ship it")
        e["key"] = "deadbeefdeadbeef"
        body = intake._body_for(e)
        self.assertIn("<!-- intake\nrisk: low\nbet: b-1\n"
                      "source: intake:deadbeefdeadbeef\n-->", body)
        self.assertEqual(body.count("bet:"), 1)

    # --- the approvals section is untouched ---------------------------------
    def test_bet_is_not_an_approval_verb_with_the_flag_on(self):
        """Row 82 dropped `approve pr:` for good; the `bet:` APPROVAL verb is
        parked on "first bet stamped". A bare `- bet: x` line must stay
        unparseable so nothing the phone can write gains a privilege."""
        os.environ["OPS_BET_FIELD"] = "1"
        e = self._parse("- bet: b-1")
        self.assertEqual(e["verb"], "error")
        self.assertIn("not an instruction the bridge understands", e["why"])

    def test_approve_spec_is_unaffected(self):
        os.environ["OPS_BET_FIELD"] = "1"
        e = self._parse("- approve spec: menu-bar")
        self.assertEqual(e, {"verb": "approve_spec", "slug": "menu-bar"})

    def test_the_grammar_error_still_names_the_three_verbs(self):
        e = self._parse("- nonsense")
        self.assertIn("repo: / approve spec: / drop:", e["why"])


class PlanNext(unittest.TestCase):
    """Row 201 / R4: `## Plan next` is the owner's override of the sprint
    planner's pick order. It is read-only from the bridge's point of view -
    the whole point of `test_consumable_is_unchanged` below."""

    def test_consumable_is_unchanged(self):
        """`## Plan next` must never become work the bridge creates - the
        same reason `## Findings` is absent from CONSUMABLE."""
        self.assertEqual(intake.CONSUMABLE, ("## New work", "## Approvals"))
        self.assertNotIn(intake.PLAN_NEXT, intake.CONSUMABLE)

    def test_no_heading_is_an_empty_list(self):
        self.assertEqual(intake.plan_next("---\ntype: intake\n---\n## New work\n"), [])

    def test_heading_with_no_lines_is_an_empty_list(self):
        self.assertEqual(intake.plan_next(f"{intake.PLAN_NEXT}\n## Notes\n"), [])

    def test_one_entry_with_no_note(self):
        text = f"{intake.PLAN_NEXT}\n- my-app t_00000009\n## Notes\n"
        self.assertEqual(intake.plan_next(text), [("my-app", "t_00000009", "")])

    def test_two_entries_one_with_a_note_in_file_order(self):
        text = (f"{intake.PLAN_NEXT}\n"
                "- my-app t_00000009 | waiting since 2026-09-09\n"
                "- capsule-corp t_0000000a\n"
                "## Notes\n")
        self.assertEqual(intake.plan_next(text),
                         [("my-app", "t_00000009", "waiting since 2026-09-09"),
                          ("capsule-corp", "t_0000000a", "")])

    def test_a_malformed_line_is_ignored_not_an_error(self):
        for bad in ("- my-app", "- my-app not-a-task-id", "- MY-APP t_00000009",
                    "- pomo_doro t_00000009", "not even a list line"):
            with self.subTest(bad=bad):
                text = f"{intake.PLAN_NEXT}\n{bad}\n- my-app t_00000009\n## Notes\n"
                self.assertEqual(intake.plan_next(text), [("my-app", "t_00000009", "")])

    def test_lines_outside_the_heading_are_never_read(self):
        text = ("## New work\n- my-app t_11111111\n"
                f"{intake.PLAN_NEXT}\n- my-app t_00000009\n"
                "## Notes\n- my-app t_22222222\n")
        self.assertEqual(intake.plan_next(text), [("my-app", "t_00000009", "")])

    def test_the_heading_is_case_and_whitespace_insensitive(self):
        """Review Finding 6: a hand-typed heading (RUNBOOK tells the owner to
        type it himself) must not silently go dead over a trailing space or
        a capitalization slip."""
        for heading in ("## Plan next ", "##  Plan next", "## PLAN NEXT",
                        "##\tplan next", "## plan Next   "):
            with self.subTest(heading=heading):
                text = f"{heading}\n- my-app t_00000009\n## Notes\n"
                self.assertEqual(intake.plan_next(text), [("my-app", "t_00000009", "")])

    def test_a_heading_with_no_space_after_the_hashes_is_not_the_heading(self):
        """The tolerance is whitespace/case around an otherwise-correct
        heading, not a wholly different shape - `##Plan next` (no space) is
        just an unrecognised heading, same as any other unknown one."""
        text = "##Plan next\n- my-app t_00000009\n## Notes\n"
        self.assertEqual(intake.plan_next(text), [])

    def test_only_the_first_matching_heading_is_read(self):
        text = (f"{intake.PLAN_NEXT}\n- my-app t_00000009\n"
                f"## Notes\n{intake.PLAN_NEXT}\n- capsule-corp t_0000000a\n")
        self.assertEqual(intake.plan_next(text), [("my-app", "t_00000009", "")])

    def test_only_the_two_consumable_sections_are_parsed_not_plan_next(self):
        """Row 201 / R4 review Finding 9: the invariant that matters is not
        `CONSUMABLE`'s value but that `parse()` - the bridge's own reader -
        structurally never returns a `## Plan next` entry, consumable or
        not."""
        text = (f"{intake.NEW_WORK}\n- repo: my-app | risk: med | Keep this one\n"
                f"{intake.PLAN_NEXT}\n- my-app t_00000009 | waiting since 2026-09-09\n"
                f"{intake.APPROVALS}\n- approve spec: retention-sweep\n")
        entries = intake.parse(text, boards=BOARDS)
        self.assertEqual([e["section"] for e in entries], [intake.NEW_WORK, intake.APPROVALS])
        for e in entries:
            self.assertNotIn("t_00000009", e["raw"])
            self.assertNotIn("Plan next", e["raw"])


class RemovePlanNext(unittest.TestCase):
    def test_removes_the_named_line_only(self):
        text = (f"{intake.PLAN_NEXT}\n"
                "- my-app t_00000009 | waiting since 2026-09-09\n"
                "- capsule-corp t_0000000a\n"
                "## Notes\n- keep me\n")
        out, removed = intake.remove_plan_next(text, "my-app", "t_00000009")
        self.assertTrue(removed)
        self.assertEqual(out, f"{intake.PLAN_NEXT}\n- capsule-corp t_0000000a\n## Notes\n- keep me\n")

    def test_a_missing_entry_is_a_no_op(self):
        text = f"{intake.PLAN_NEXT}\n- my-app t_00000009\n## Notes\n"
        out, removed = intake.remove_plan_next(text, "capsule-corp", "t_0000000a")
        self.assertFalse(removed)
        self.assertEqual(out, text)

    def test_no_heading_is_a_no_op(self):
        text = "## New work\n"
        out, removed = intake.remove_plan_next(text, "my-app", "t_00000009")
        self.assertFalse(removed)
        self.assertEqual(out, text)

    def test_other_sections_are_untouched(self):
        text = (f"## New work\n- repo: my-app | keep\n{intake.PLAN_NEXT}\n"
                "- my-app t_00000009\n## Notes\n- keep me\n")
        out, removed = intake.remove_plan_next(text, "my-app", "t_00000009")
        self.assertTrue(removed)
        self.assertIn("- repo: my-app | keep", out)
        self.assertIn("- keep me", out)

    def test_a_tolerant_heading_is_still_found(self):
        text = "##  Plan Next \n- my-app t_00000009\n## Notes\n"
        out, removed = intake.remove_plan_next(text, "my-app", "t_00000009")
        self.assertTrue(removed)
        self.assertEqual(out, "##  Plan Next \n## Notes\n")

    # --- review Finding 7: EOF bytes outside the touched section survive ---
    def test_no_trailing_newline_at_eof_is_preserved(self):
        text = f"{intake.PLAN_NEXT}\n- my-app t_00000009\n## Notes\n- keep me"
        out, removed = intake.remove_plan_next(text, "my-app", "t_00000009")
        self.assertTrue(removed)
        self.assertEqual(out, f"{intake.PLAN_NEXT}\n## Notes\n- keep me")
        self.assertFalse(out.endswith("\n"))

    def test_a_blank_line_at_eof_is_preserved(self):
        text = f"{intake.PLAN_NEXT}\n- my-app t_00000009\n## Notes\n- keep me\n\n\n"
        out, removed = intake.remove_plan_next(text, "my-app", "t_00000009")
        self.assertTrue(removed)
        self.assertEqual(out, f"{intake.PLAN_NEXT}\n## Notes\n- keep me\n\n\n")

    def test_crlf_line_endings_are_preserved(self):
        text = f"{intake.PLAN_NEXT}\r\n- my-app t_00000009\r\n## Notes\r\n- keep me\r\n"
        out, removed = intake.remove_plan_next(text, "my-app", "t_00000009")
        self.assertTrue(removed)
        self.assertEqual(out, f"{intake.PLAN_NEXT}\r\n## Notes\r\n- keep me\r\n")

    def test_the_plan_next_section_at_eof_with_no_trailing_newline(self):
        """No `## Notes` after it at all - the section runs to EOF, and EOF
        itself carries no newline."""
        text = f"{intake.PLAN_NEXT}\n- my-app t_00000009\n- capsule-corp t_0000000a"
        out, removed = intake.remove_plan_next(text, "capsule-corp", "t_0000000a")
        self.assertTrue(removed)
        self.assertEqual(out, f"{intake.PLAN_NEXT}\n- my-app t_00000009\n")


class PlanNextCli(unittest.TestCase):
    def setUp(self):
        self.vault = Path(tempfile.mkdtemp(prefix="pn-vault-"))
        (self.vault / "Boards").mkdir()
        self.path = self.vault / "Boards" / "Intake.md"

    def _write(self, text):
        self.path.write_text(text, encoding="utf-8")

    def _cli(self, argv):
        import io, contextlib
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = intake.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_plan_next_prints_one_line_per_entry(self):
        self._write(f"{intake.PLAN_NEXT}\n"
                   "- my-app t_00000009 | waiting since 2026-09-09\n"
                   "- capsule-corp t_0000000a\n## Notes\n")
        rc, out, err = self._cli(["plan-next", "--vault", str(self.vault)])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out.splitlines(),
                         ["my-app t_00000009 | waiting since 2026-09-09",
                          "capsule-corp t_0000000a"])

    def test_plan_next_prints_nothing_when_empty(self):
        self._write("## New work\n")
        rc, out, err = self._cli(["plan-next", "--vault", str(self.vault)])
        self.assertEqual((rc, out, err), (0, "", ""))

    def test_plan_next_missing_file_reads_as_empty(self):
        rc, out, err = self._cli(["plan-next", "--vault", str(self.vault)])
        self.assertEqual((rc, out, err), (0, "", ""))

    def test_plan_next_remove_writes_and_exits_0(self):
        self._write(f"{intake.PLAN_NEXT}\n- my-app t_00000009\n## Notes\n")
        rc, out, err = self._cli(["plan-next-remove", "my-app", "t_00000009",
                                  "--vault", str(self.vault)])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("removed", out)
        self.assertNotIn("t_00000009", self.path.read_text(encoding="utf-8"))

    def test_plan_next_remove_is_idempotent(self):
        self._write(f"{intake.PLAN_NEXT}\n## Notes\n")
        rc, out, err = self._cli(["plan-next-remove", "my-app", "t_00000009",
                                  "--vault", str(self.vault)])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("not in", out)

    def test_plan_next_remove_refuses_the_default_vault_under_test(self):
        self.assertTrue(os.environ.get("AGENT_OPS_NO_SEND"))
        with self.assertRaises(RuntimeError):
            intake.apply_remove_plan_next("my-app", "t_00000009")

    # --- review Finding 1: --vault is test-only on both verbs -------------
    def _without_no_send(self, argv):
        old = os.environ.pop("AGENT_OPS_NO_SEND", None)
        try:
            return self._cli(argv)
        finally:
            if old is not None:
                os.environ["AGENT_OPS_NO_SEND"] = old

    def test_plan_next_refuses_vault_without_agent_ops_no_send(self):
        rc, out, err = self._without_no_send(["plan-next", "--vault", str(self.vault)])
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("--vault is test-only", err)

    def test_plan_next_remove_refuses_vault_without_agent_ops_no_send(self):
        self._write(f"{intake.PLAN_NEXT}\n- my-app t_00000009\n## Notes\n")
        rc, out, err = self._without_no_send(["plan-next-remove", "my-app", "t_00000009",
                                              "--vault", str(self.vault)])
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("--vault is test-only", err)
        # refused before any write happens
        self.assertIn("t_00000009", self.path.read_text(encoding="utf-8"))

    def test_plan_next_without_vault_flag_still_works_outside_tests(self):
        """The refusal is on the `--vault` FLAG, not on running the command at
        all - production never passes --vault (it relies on
        $OBSIDIAN_VAULT_PATH) and must keep working with AGENT_OPS_NO_SEND
        unset. `board_view.VAULT` is bound at import time, so the env var
        itself can't be exercised here without reimporting the module; mock
        `vault_root` instead to prove the guard never fires when `ns.vault`
        is None."""
        import board_view
        self._write(f"{intake.PLAN_NEXT}\n- my-app t_00000009\n## Notes\n")
        with unittest.mock.patch.object(board_view, "vault_root",
                                        lambda vault=None: self.vault if vault is None else None):
            rc, out, err = self._without_no_send(["plan-next"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out.splitlines(), ["my-app t_00000009"])
