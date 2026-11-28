# mutation-check: targets-predate-range b957739c9 -- rename only, no behaviour pinned ::
#   Render.test_every_card_line_carries_its_raw_status
import datetime as dt
import os, sys, tempfile, unittest, unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["AGENT_OPS_NO_SEND"] = "1"
os.environ.setdefault("EVENTS_SPOOL", str(Path(tempfile.mkdtemp(prefix="events-view-")) / "events.jsonl"))
import board
import board_view

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TODAY = dt.date(2026, 9, 7)


def card(board_name, tid, title, status, *, body="", comments=(), runs=(), assignee=None,
         children=(), latest_summary=""):
    return board.Card(board_name, {"task": {"id": tid, "title": title, "status": status, "body": body,
                                            "assignee": assignee},
                                   "comments": list(comments), "runs": list(runs),
                                   "children": list(children), "latest_summary": latest_summary})


def _sample():
    """One card per column, across two repos, so the golden file exercises
    every heading and the repo grouping at once."""
    return [
        card("my-app", "t_0000000a", "Add a weekly summary email of finished sessions", "triage"),
        card("capsule-corp", "t_0000000b", "Investigate why the nightly report is late", "triage"),
        card("my-app", "t_0000000c", "Retention sweep runs on the first poll cycle", "triage",
             body="spec: Specs/retention-sweep-first-cycle-prune-cli.md"),
        card("my-app", "t_0000000d", "Soft tick toggle on the timer page", "todo",
             body="spec: Specs/soft-tick-toggle.md"),
        # `assignee="dev"` (row 2026-09-11 hotfix): a bare `ready` card with
        # no assignee is now an undispatched source card, which would
        # classify as Spec Ready/Approved, not Sprint. An assignee is what a
        # dispatch's impl/verify/lead cards carry.
        card("my-app", "t_0000000e", "Session history sparkline", "ready",
             body="spec: Specs/session-history-sparkline.md", assignee="dev"),
        card("my-app", "t_0000000f", "Plan-usage gauges on Home", "running",
             body="spec: Specs/plan-usage-gauges-home.md"),
        card("my-app", "t_00000010", "Change-only snapshot persistence", "review",
             body="spec: Specs/change-only-snapshot-persistence.md"),
        card("capsule-corp", "t_00000011", "Run the dashboard as a launchd service", "blocked",
             comments=[{"body": "BLOCKED: NEEDS REPAIR: the suite died three times", "created_at": 5}]),
        card("my-app", "t_00000012", "Rewire the settings pane", "blocked",
             comments=[{"body": "BLOCKED: DECISION: which storage?", "created_at": 5}]),
        card("my-app", "t_00000013", "Timer page ships", "done",
             runs=[{"outcome": "completed", "ended_at": 1788757200.0}]),
    ]


def _done(tid, *, days_ago=None, board_name="my-app", title=None, comments=()):
    """A done card finished `days_ago` days before TODAY, in UTC. days_ago=None
    means no run stamp at all (a card completed before runs were stamped)."""
    runs = ()
    if days_ago is not None:
        when = dt.datetime.combine(TODAY - dt.timedelta(days=days_ago), dt.time(12, 0),
                                   tzinfo=dt.timezone.utc)
        runs = ({"outcome": "completed", "ended_at": when.timestamp()},)
    return card(board_name, tid, title or f"Card {tid}", "done", runs=runs, comments=comments)


class DoneWindow(unittest.TestCase):
    """Row 84: the board shows a 14-day window of Done, not a 30-card cap.
    The boundary is computed from the injected `today`, never a clock, so the
    file is byte-stable within a day."""

    def test_the_cap_is_gone(self):
        self.assertFalse(hasattr(board_view, "DONE_CAP"))
        self.assertEqual(board_view.DONE_WINDOW_DAYS, 14)

    def test_a_card_done_thirteen_days_ago_renders(self):
        out = board_view.render_board([_done("t_0000000a", days_ago=13)], today=TODAY)
        self.assertIn("t_0000000a", out)

    def test_a_card_done_fifteen_days_ago_does_not(self):
        out = board_view.render_board([_done("t_0000000b", days_ago=15)], today=TODAY)
        self.assertNotIn("t_0000000b", out)
        self.assertIn("<!-- 1 older cards: see Boards/Archive.md -->", out)

    def test_the_boundary_day_itself_renders(self):
        out = board_view.render_board([_done("t_0000000c", days_ago=14)], today=TODAY)
        self.assertIn("t_0000000c", out)

    def test_an_unstamped_done_card_lives_in_the_archive_only(self):
        out = board_view.render_board([_done("t_0000000d", days_ago=None)], today=TODAY)
        self.assertNotIn("t_0000000d", out)
        self.assertIn("<!-- 1 older cards: see Boards/Archive.md -->", out)

    def test_the_pointer_counts_only_what_the_window_hid(self):
        cards = [_done(f"t_{i:08x}", days_ago=1) for i in range(3)] + \
                [_done(f"t_{i:08x}", days_ago=40) for i in range(3, 10)]
        out = board_view.render_board(cards, today=TODAY)
        self.assertIn("<!-- 7 older cards: see Boards/Archive.md -->", out)

    def test_nothing_hidden_means_no_pointer(self):
        out = board_view.render_board([_done("t_0000000e", days_ago=2)], today=TODAY)
        self.assertNotIn("older cards", out)

    def test_the_window_never_reads_the_clock(self):
        """Two renders with different `today` must differ; two with the same
        `today` must be byte-identical whatever the wall clock says."""
        cards = [_done("t_0000000f", days_ago=13)]
        near = board_view.render_board(cards, today=TODAY)
        far = board_view.render_board(cards, today=TODAY + dt.timedelta(days=10))
        self.assertIn("t_0000000f", near)
        self.assertNotIn("t_0000000f", far)
        self.assertEqual(board_view.render_board(cards, today=TODAY), near)


class NoDateLine(unittest.TestCase):
    """Row 85: the frontmatter carried `generated: <date>`, so the file
    changed once a day even when nothing on the board did."""

    def test_the_frontmatter_has_no_generated_date(self):
        out = board_view.render_board(_sample(), today=TODAY)
        self.assertNotIn("generated:", out)
        self.assertIn("kanban-plugin: board", out)

    def test_two_days_render_identical_bytes(self):
        a = board_view.render_board(_sample(), today=TODAY)
        b = board_view.render_board(_sample(), today=TODAY + dt.timedelta(days=1))
        self.assertEqual(a, b)

    def test_the_golden_board_has_no_generated_line(self):
        self.assertNotIn("generated:",
                         (FIXTURES / "engineering-generated.md").read_text(encoding="utf-8"))


class RenderArchive(unittest.TestCase):
    """Row 84: the whole Done history as a flat list, grouped by month. Not a
    Kanban board - no `kanban-plugin` key, so the plugin ignores it."""

    def test_it_matches_the_golden_file(self):
        got = board_view.render_archive(_sample(), today=TODAY)
        want = (FIXTURES / "archive-generated.md").read_text(encoding="utf-8")
        self.assertEqual(got, want)

    def test_the_golden_archive_line_shape(self):
        want = ("- 2026-09-07 · my-app · **Timer page ships** · - · "
                "<!-- kanban:my-app/t_00000013 -->")
        self.assertIn(want, (FIXTURES / "archive-generated.md").read_text(encoding="utf-8"))

    def test_it_is_not_a_kanban_board(self):
        out = board_view.render_archive(_sample(), today=TODAY)
        self.assertNotIn("kanban-plugin", out)
        self.assertNotIn("kanban:settings", out)
        self.assertIn("type: archive", out)

    def test_it_carries_no_generated_date(self):
        self.assertNotIn("generated:", board_view.render_archive(_sample(), today=TODAY))

    def test_only_done_cards_appear(self):
        out = board_view.render_archive(_sample(), today=TODAY)
        self.assertIn("t_00000013", out)          # the sample's one done card
        self.assertNotIn("t_0000000a", out)       # a triage card

    def test_render_archive_renders_a_helper_handed_to_it_collect_is_the_filter(self):
        """`collect` drops them before either renderer sees them, so the
        archive inherits the exclusion - this pins that it is not re-decided
        here with a different rule."""
        helper = card("my-app", "t_00000099", "Helper", "done",
                      body=f"{board.HELPER_MARKER} t_00000013",
                      runs=[{"ended_at": 1_788_757_200.0}])
        self.assertIn("t_00000099", board_view.render_archive([helper], today=TODAY))

    def test_cards_are_grouped_by_the_month_they_finished_newest_first(self):
        cards = [_done("t_00000001", days_ago=1), _done("t_00000002", days_ago=40),
                 _done("t_00000003", days_ago=45)]
        out = board_view.render_archive(cards, today=TODAY)
        heads = [l for l in out.splitlines() if l.startswith("## ")]
        self.assertEqual(heads, ["## 2026-09", "## 2026-07"])

    def test_unstamped_cards_go_last_under_undated(self):
        cards = [_done("t_00000001", days_ago=1), _done("t_00000002", days_ago=None)]
        out = board_view.render_archive(cards, today=TODAY)
        heads = [l for l in out.splitlines() if l.startswith("## ")]
        self.assertEqual(heads, ["## 2026-09", board_view.ARCHIVE_UNDATED])
        self.assertIn("- - · my-app · **Card t_00000002**", out)

    def test_the_line_carries_the_same_join_key_as_the_board(self):
        c = _done("t_00000001", days_ago=1)
        self.assertIn("<!-- kanban:my-app/t_00000001 -->", board_view.archive_line(c))
        self.assertIn("<!-- kanban:my-app/t_00000001 -->", board_view.card_line(c))

    def test_the_pr_url_is_rendered_when_the_card_carries_one(self):
        c = _done("t_00000001", days_ago=1,
                  comments=[{"body": "Merged - https://github.com/x/y/pull/42", "created_at": 3}])
        self.assertIn("https://github.com/x/y/pull/42", board_view.archive_line(c))

    def test_a_card_with_no_pr_renders_a_dash(self):
        self.assertIn(" · - · ", board_view.archive_line(_done("t_00000001", days_ago=1)))

    def test_the_pr_url_walks_the_chain_to_the_finalize_card(self):
        """Fix round 1, I1: a real merge posts `merged <url>` on the FINALIZE
        (`assignee: lead`) card, not the impl card the Archive line is for.
        board_view must not import board_bridge (board_bridge imports
        board_view), so the lookup walks the same `cards` list `collect()`
        already returned - `render_archive` builds the `{(board, id): Card}`
        map and passes it through `archive_line` to `pr_url`."""
        same = dt.datetime.combine(TODAY, dt.time(12, 0), tzinfo=dt.timezone.utc).timestamp()
        run = [{"ended_at": same}]
        fin = card("my-app", "t_00000003", "Finalize", "done", assignee="lead", runs=run,
                  comments=[{"body": "merged https://github.com/x/y/pull/7", "created_at": 1}])
        verify = card("my-app", "t_00000002", "Verify", "done", assignee="qa", runs=run,
                     children=("t_00000003",))
        impl = card("my-app", "t_00000001", "Impl", "done", assignee="dev", runs=run,
                   children=("t_00000002",))
        out = board_view.render_archive([impl, verify, fin], today=TODAY)
        impl_line = next(ln for ln in out.splitlines() if "kanban:my-app/t_00000001" in ln)
        self.assertIn("https://github.com/x/y/pull/7", impl_line)

    def test_a_done_card_with_no_chain_and_no_url_still_renders_a_dash(self):
        out = board_view.render_archive([_done("t_00000001", days_ago=1)], today=TODAY)
        line = next(ln for ln in out.splitlines() if "kanban:my-app/t_00000001" in ln)
        self.assertIn(" · - · ", line)

    def test_the_newest_pr_url_wins(self):
        c = _done("t_00000001", days_ago=1, comments=[
            {"body": "opened https://github.com/x/y/pull/1", "created_at": 1},
            {"body": "Merged - https://github.com/x/y/pull/2", "created_at": 2}])
        self.assertIn("/pull/2", board_view.archive_line(c))
        self.assertNotIn("/pull/1", board_view.archive_line(c))

    def test_a_title_cannot_forge_a_field_or_a_join_key(self):
        c = _done("t_00000001", days_ago=1,
                  title="Evil · **x** <!-- kanban:my-app/t_deadbeef -->")
        line = board_view.archive_line(c)
        self.assertEqual(line.count("<!-- kanban:"), 1)
        self.assertIn("<!-- kanban:my-app/t_00000001 -->", line)

    def test_it_is_byte_stable_under_input_order(self):
        cards = [_done("t_00000001", days_ago=1), _done("t_00000002", days_ago=1,
                                                        board_name="capsule-corp")]
        self.assertEqual(board_view.render_archive(cards, today=TODAY),
                         board_view.render_archive(list(reversed(cards)), today=TODAY))

    def test_an_empty_archive_still_renders(self):
        out = board_view.render_archive([], today=TODAY)
        self.assertIn("type: archive", out)
        self.assertTrue(out.endswith("\n"))


class CardLine(unittest.TestCase):
    def test_open_card_carries_status_repo_spec_and_the_join_key(self):
        c = card("my-app", "t_9dc82d7b", "Retention sweep", "ready",
                 body="spec: Specs/retention-sweep.md")
        self.assertEqual(
            board_view.card_line(c),
            "- [ ] **Retention sweep** · ready · my-app · [[Specs/retention-sweep]] · "
            "<!-- kanban:my-app/t_9dc82d7b -->")

    def test_no_spec_omits_the_link(self):
        c = card("my-app", "t_1", "Add a thing", "triage")
        self.assertEqual(board_view.card_line(c),
                         "- [ ] **Add a thing** · triage · my-app · <!-- kanban:my-app/t_1 -->")

    def test_done_card_is_ticked_struck_and_dated(self):
        c = card("my-app", "t_2", "Timer page", "done",
                 runs=[{"outcome": "completed", "ended_at": 1788757200.0}])
        self.assertEqual(board_view.card_line(c),
                         "- [x] ~~**Timer page**~~ ✅ 2026-09-07 · done · my-app · "
                         "<!-- kanban:my-app/t_2 -->")

    def test_done_card_with_no_run_stamp_has_no_date(self):
        c = card("my-app", "t_2", "Timer page", "done")
        self.assertIsNone(board_view.done_at(c))
        self.assertEqual(board_view.card_line(c),
                         "- [x] ~~**Timer page**~~ · done · my-app · <!-- kanban:my-app/t_2 -->")

    def test_a_hostile_title_cannot_break_a_line(self):
        title = "Fix **bold** and\na newline and <!-- kanban:my-app/t_evil --> too"
        c = card("my-app", "t_3", title, "ready")
        line = board_view.card_line(c)
        self.assertEqual(line.count("<!-- kanban:"), 1)
        self.assertNotIn("\n", line)
        self.assertIn("Fix bold and a newline and (comment) too", line)

    def test_a_long_title_is_truncated_to_120_chars(self):
        c = card("my-app", "t_4", "x" * 400, "ready")
        self.assertIn("x" * 119 + "…", board_view.card_line(c))


class Render(unittest.TestCase):
    def test_matches_the_golden_file_byte_for_byte(self):
        got = board_view.render_board(_sample(), today=TODAY)
        want = (FIXTURES / "engineering-generated.md").read_text(encoding="utf-8")
        self.assertEqual(got, want)

    def test_rendering_twice_is_byte_identical(self):
        a = board_view.render_board(_sample(), today=TODAY)
        b = board_view.render_board(list(reversed(_sample())), today=TODAY)
        self.assertEqual(a, b)

    def test_every_column_heading_appears_exactly_once_and_in_order(self):
        out = board_view.render_board(_sample(), today=TODAY)
        idx = [out.index(h) for h in board.COLUMN_NAMES]
        self.assertEqual(idx, sorted(idx))
        for h in board.COLUMN_NAMES:
            self.assertEqual(out.count("\n" + h + "\n"), 1, h)

    def test_an_empty_board_still_renders_every_column(self):
        out = board_view.render_board([], today=TODAY)
        for h in board.COLUMN_NAMES:
            self.assertIn(h, out)
        self.assertIn("kanban-plugin: board", out)
        self.assertIn(board_view.SETTINGS_BLOCK, out)

    def test_adding_a_card_diffs_exactly_one_column(self):
        before = board_view.render_board(_sample(), today=TODAY).splitlines()
        new = card("my-app", "t_00000099", "New idea", "triage")
        after = board_view.render_board(_sample() + [new],
                                        today=TODAY).splitlines()
        added = [l for l in after if l not in before]
        self.assertEqual(added, ["- [ ] **New idea** · triage · my-app · "
                                   "<!-- kanban:my-app/t_00000099 -->"])

    def test_every_card_line_carries_its_raw_status(self):
        out = board_view.render_board(_sample(), today=TODAY)
        for c in _sample():
            self.assertIn(f"· {c.legacy_status} ·", out)

    def test_repo_runs_contiguous_within_a_column(self):
        cards = [card("my-app", "t_02", "b", "ready", assignee="dev"),
                 card("capsule-corp", "t_01", "a", "ready", assignee="dev"),
                 card("my-app", "t_03", "c", "ready", assignee="dev")]
        sprint = board_view.render_board(cards, today=TODAY).split("## \U0001f3c3 Sprint\n", 1)[1]
        repos = [l.split(" · ")[2] for l in sprint.splitlines() if l.startswith("- [")]
        self.assertEqual(repos, ["capsule-corp", "my-app", "my-app"])

    def test_archived_cards_are_not_rendered(self):
        out = board_view.render_board([card("my-app", "t_dead", "Gone", "archived")], today=TODAY)
        self.assertNotIn("t_dead", out)

    def test_done_ties_break_ascending_like_every_other_lane(self):
        """M2: one convention. `reverse=True` on the whole tuple also reversed
        the board/id tie-break, so two cards finished in the same second came
        out backwards from the order the other lanes use. An unstamped card
        is archive-only under the window, so it is no longer in this list."""
        same = dt.datetime.combine(TODAY, dt.time(12, 0), tzinfo=dt.timezone.utc).timestamp()
        cards = [card("my-app", "t_00000002", "Bee", "done", runs=[{"ended_at": same}]),
                 card("capsule-corp", "t_00000003", "Cee", "done", runs=[{"ended_at": same}]),
                 card("my-app", "t_00000001", "Aye", "done", runs=[{"ended_at": same}]),
                 card("my-app", "t_00000009", "Older", "done", runs=[{"ended_at": same - 10}]),
                 card("my-app", "t_00000000", "Unstamped", "done")]
        done = board_view.render_board(cards, today=TODAY).split("## ✅ Done\n", 1)[1]
        titles = [l.split("~~**", 1)[1].split("**~~", 1)[0]
                  for l in done.splitlines() if l.startswith("- [")]
        self.assertEqual(titles, ["Cee", "Aye", "Bee", "Older"])
        self.assertIn("<!-- 1 older cards: see Boards/Archive.md -->", done)

    def test_the_done_date_does_not_depend_on_the_host_timezone(self):
        """M1: rendered in UTC, so the same card renders the same byte in
        Bangalore and in California."""
        import datetime as dt
        ts = 1_788_800_000.0    # 22:13 UTC - a different local day west of it
        line = board_view.card_line(card("my-app", "t_1", "X", "done",
                                         runs=[{"ended_at": ts}]))
        want = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).date().isoformat()
        self.assertIn(f" ✅ {want}", line)

    def test_under_the_cap_there_is_no_pointer(self):
        done = board_view.render_board(_sample(), today=TODAY).split("## ✅ Done\n", 1)[1]
        self.assertNotIn("older cards", done)

    def test_settings_list_collapse_is_sized_to_the_columns(self):
        self.assertEqual(board_view.SETTINGS_BLOCK.count("false"), len(board.COLUMNS))

    def test_the_banner_points_at_intake(self):
        self.assertIn("Boards/Intake.md", board_view.render_board([], today=TODAY))


sys.path.insert(0, str(Path(__file__).resolve().parent))
import importlib.util  # noqa: E402
# maintenance/tests is a package, so simplicity rule 1 does not count `fakes` as a bare repo
# module; load it by path, as test_e2e_pipeline does.
_FAKES_SPEC = importlib.util.spec_from_file_location(
    "fakes", Path(__file__).resolve().parent / "fakes.py")
fakes = importlib.util.module_from_spec(_FAKES_SPEC)
sys.modules["fakes"] = fakes
_FAKES_SPEC.loader.exec_module(fakes)
FakeLegacyCli = fakes.FakeLegacyCli


def _boards_dir(*names):
    root = Path(tempfile.mkdtemp(prefix="bv-boards-"))
    for n in names:
        (root / n).mkdir(parents=True)
        (root / n / "kanban.db").write_text("")
    return root


class Collect(unittest.TestCase):
    def setUp(self):
        self.dir = _boards_dir("my-app")
        self.h = FakeLegacyCli(self.dir)
        self.h.board("my-app").create(board.NewCard(title="One"))
        self.h.board("my-app").create(board.NewCard(title="Two"))

    def test_collects_live_cards_from_every_board(self):
        cards = board_view.collect(boards_dir=self.dir, runner=self.h)
        self.assertEqual(sorted(c.title for c in cards), ["One", "Two"])
        self.assertTrue(all(c.board == "my-app" for c in cards))

    def test_archived_cards_are_not_collected(self):
        tid = [c["id"] for c in self.h.created][0]
        self.h.board("my-app").archive(tid)
        self.assertEqual([c.title for c in board_view.collect(boards_dir=self.dir, runner=self.h)], ["Two"])

    def test_a_card_that_fails_to_show_is_skipped_not_fatal(self):
        real = self.h.__call__

        def flaky(argv, env=None):
            if "show" in argv and "t_00000001" in argv:
                raise OSError("boom")
            return real(argv, env)

        cards = board_view.collect(boards_dir=self.dir, runner=flaky)
        self.assertEqual([c.title for c in cards], ["Two"])

    def test_no_boards_dir_gives_no_cards(self):
        self.assertEqual(board_view.collect(boards_dir=Path(tempfile.mkdtemp()), runner=self.h), [])

    def test_helper_cards_are_excluded(self):
        self.h.board("my-app").create(board.NewCard(title="Helper", body="helper-of: t_00000001"))
        cards = board_view.collect(boards_dir=self.dir, runner=self.h)
        self.assertEqual(sorted(c.title for c in cards), ["One", "Two"])


class Regenerate(unittest.TestCase):
    def setUp(self):
        self.dir = _boards_dir("my-app")
        self.h = FakeLegacyCli(self.dir)
        self.h.board("my-app").create(board.NewCard(title="One"))
        self.vault = Path(tempfile.mkdtemp(prefix="bv-vault-"))
        (self.vault / "Boards").mkdir()
        self.pushed = []

    def _push(self, cmd):
        self.pushed.append(list(cmd))
        # `diff --cached --quiet` reports non-zero (staged differences exist)
        # so vault_git.push actually commits - the same shape a real repo
        # with a first-ever write produces, which is what lets
        # test_both_files_are_pushed_in_one_commit see the commit it checks.
        rc = 1 if "diff" in cmd else 0
        return type("R", (), {"returncode": rc, "stdout": "", "stderr": ""})()

    def test_writes_the_file_and_returns_its_path(self):
        p = board_view.regenerate(vault=self.vault, boards_dir=self.dir, runner=self.h,
                                  today=TODAY, git_runner=self._push)
        self.assertEqual(p, self.vault / "Boards" / "Engineering.md")
        self.assertIn("**One**", p.read_text(encoding="utf-8"))

    def test_a_second_tick_with_no_change_rewrites_identical_bytes(self):
        p = board_view.regenerate(vault=self.vault, boards_dir=self.dir, runner=self.h,
                                  today=TODAY, git_runner=self._push)
        first = p.read_bytes()
        board_view.regenerate(vault=self.vault, boards_dir=self.dir, runner=self.h,
                              today=TODAY, git_runner=self._push)
        self.assertEqual(p.read_bytes(), first)

    def test_it_pulls_before_and_pushes_only_the_board_file(self):
        board_view.regenerate(vault=self.vault, boards_dir=self.dir, runner=self.h,
                              today=TODAY, git_runner=self._push)
        joined = [" ".join(c) for c in self.pushed]
        self.assertTrue(any("pull" in j for j in joined))
        add = next(j for j in joined if " add " in j)
        self.assertIn("Boards/Engineering.md", add)
        self.assertNotIn("Boards/Intake.md", add)

    def test_the_default_today_is_utc_not_the_host_zone(self):
        """M1: `done_date`, the Done-window boundary and the archive's month
        heading are all UTC; a local `date.today()` here moved the window a day
        for a few hours every evening and churned both files."""
        seen = {}
        real = board_view.render_board

        def spy(cards, *, today):
            seen["today"] = today
            return real(cards, today=today)

        with unittest.mock.patch.object(board_view, "render_board", spy):
            board_view.regenerate(vault=self.vault, boards_dir=self.dir, runner=self.h,
                                  git_runner=self._push)
        self.assertEqual(seen["today"], dt.datetime.now(dt.timezone.utc).date())

    def test_an_injected_today_is_still_used_verbatim(self):
        """The injected path is what every other test and the goldens rely on:
        the UTC default must apply only when `today` is not given."""
        seen = {}
        real = board_view.render_board

        def spy(cards, *, today):
            seen["today"] = today
            return real(cards, today=today)

        with unittest.mock.patch.object(board_view, "render_board", spy):
            board_view.regenerate(vault=self.vault, boards_dir=self.dir, runner=self.h,
                                  today=TODAY, git_runner=self._push)
        self.assertEqual(seen["today"], TODAY)

    def test_a_failed_pull_still_writes_locally(self):
        def failing(cmd):
            rc = 1 if "pull" in cmd else 0
            return type("R", (), {"returncode": rc, "stdout": "", "stderr": "diverged"})()
        p = board_view.regenerate(vault=self.vault, boards_dir=self.dir, runner=self.h,
                                  today=TODAY, git_runner=failing)
        self.assertTrue(p.exists())

    def test_no_vault_is_a_silent_no_op(self):
        self.assertIsNone(board_view.regenerate(vault=self.vault / "gone", boards_dir=self.dir,
                                                runner=self.h, today=TODAY, git_runner=self._push))

    def test_the_flag_off_is_a_no_op(self):
        os.environ["CAPSULE_BOARD_VIEW"] = "0"
        try:
            self.assertFalse(board_view.enabled())
            self.assertIsNone(board_view.regenerate(vault=self.vault, boards_dir=self.dir, runner=self.h,
                                                    today=TODAY, git_runner=self._push))
        finally:
            os.environ.pop("CAPSULE_BOARD_VIEW", None)
        self.assertTrue(board_view.enabled())

    def test_every_spelling_of_off_holds_the_generator_off(self):
        """I1: the flag is typed into ops.env by hand. An empty value is off
        too - `CAPSULE_BOARD_VIEW=` with nothing after it is what a mistyped
        ops.env line leaves behind, and it plainly means "off", not "on"."""
        old = os.environ.get("CAPSULE_BOARD_VIEW")
        try:
            for v in ("", "0", "false", "FALSE", "False", "no", "off", " off ", " 0"):
                os.environ["CAPSULE_BOARD_VIEW"] = v
                self.assertFalse(board_view.enabled(), f"{v!r} must read as off")
            for v in ("1", "true", "yes", "on", "anything"):
                os.environ["CAPSULE_BOARD_VIEW"] = v
                self.assertTrue(board_view.enabled(), f"{v!r} must read as on")
            os.environ.pop("CAPSULE_BOARD_VIEW", None)
            self.assertTrue(board_view.enabled(), "unset = on (spec 8.2)")
        finally:
            os.environ.pop("CAPSULE_BOARD_VIEW", None)
            if old is not None:
                os.environ["CAPSULE_BOARD_VIEW"] = old

    def test_it_writes_the_archive_beside_the_board(self):
        board_view.regenerate(vault=self.vault, boards_dir=self.dir, runner=self.h,
                              today=TODAY, git_runner=self._push)
        self.assertTrue((self.vault / "Boards" / "Archive.md").exists())
        self.assertIn("type: archive",
                      (self.vault / "Boards" / "Archive.md").read_text(encoding="utf-8"))

    def test_both_files_are_pushed_in_one_commit(self):
        board_view.regenerate(vault=self.vault, boards_dir=self.dir, runner=self.h,
                              today=TODAY, git_runner=self._push)
        joined = [" ".join(c) for c in self.pushed]
        adds = [j for j in joined if " add " in j]
        commits = [j for j in joined if " commit " in j]
        self.assertEqual(len(adds), 1, adds)
        self.assertEqual(len(commits), 1, commits)
        self.assertIn("Boards/Engineering.md", adds[0])
        self.assertIn("Boards/Archive.md", adds[0])
        self.assertNotIn("Boards/Intake.md", adds[0])

    def test_a_second_tick_rewrites_identical_archive_bytes(self):
        board_view.regenerate(vault=self.vault, boards_dir=self.dir, runner=self.h,
                              today=TODAY, git_runner=self._push)
        first = (self.vault / "Boards" / "Archive.md").read_bytes()
        board_view.regenerate(vault=self.vault, boards_dir=self.dir, runner=self.h,
                              today=TODAY, git_runner=self._push)
        self.assertEqual((self.vault / "Boards" / "Archive.md").read_bytes(), first)

    def test_the_flag_off_writes_neither_file(self):
        os.environ["CAPSULE_BOARD_VIEW"] = "0"
        try:
            self.assertIsNone(board_view.regenerate(vault=self.vault, boards_dir=self.dir,
                                                    runner=self.h, today=TODAY,
                                                    git_runner=self._push))
        finally:
            os.environ.pop("CAPSULE_BOARD_VIEW", None)
        self.assertFalse((self.vault / "Boards" / "Archive.md").exists())


class Migrate(unittest.TestCase):
    def setUp(self):
        self.dir = _boards_dir("my-app")
        self.h = FakeLegacyCli(self.dir)
        b = self.h.board("my-app")
        # ids t_00000001 / t_00000002 in creation order; the fixture's
        # annotations are rewritten to match so the test never hardcodes both.
        self.a = b.create(board.NewCard(title="Retention sweep runs on the first poll cycle"))
        self.bb = b.create(board.NewCard(title="Plan-usage gauges on Home"))
        self.vault = Path(tempfile.mkdtemp(prefix="mig-vault-"))
        (self.vault / "Boards").mkdir()
        text = (FIXTURES / "engineering-handwritten.md").read_text(encoding="utf-8")
        text = text.replace("t_0000000a", self.a).replace("t_0000000b", self.bb)
        (self.vault / "Boards" / "Engineering.md").write_text(text, encoding="utf-8")
        self.git = lambda cmd: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def _migrate(self):
        return board_view.migrate(vault=self.vault, boards_dir=self.dir, runner=self.h,
                                  today=TODAY, git_runner=self.git)

    def test_the_backup_is_written_verbatim(self):
        before = (self.vault / "Boards" / "Engineering.md").read_bytes()
        res = self._migrate()
        self.assertEqual(res["backup"], self.vault / "Boards" / "Engineering.pre-generated.md")
        self.assertEqual(res["backup"].read_bytes(), before)

    def test_annotated_cards_keep_their_prose_as_one_comment(self):
        self._migrate()
        bodies = [c["body"] for c in self.h.comments[self.a]]
        migrated = [b for b in bodies if b.startswith(board_view.MIGRATED_COMMENT_PREFIX)]
        self.assertEqual(len(migrated), 1)
        self.assertIn("Retention has never fired", migrated[0])

    def test_unannotated_lines_land_in_intake_verbatim(self):
        res = self._migrate()
        intake = (self.vault / "Boards" / "Intake.md").read_text(encoding="utf-8")
        self.assertIn(board_view.INTAKE_MIGRATED_HEADING(TODAY), intake)
        self.assertIn("🚫 Do not pick up", intake)
        self.assertGreater(res["intake_lines"], 0)

    def test_an_annotated_card_on_no_board_is_reported_never_dropped(self):
        res = self._migrate()
        self.assertIn("t_00badbad", [tid for tid, _ in res["orphans"]])
        self.assertIn("t_00badbad", res["backup"].read_text(encoding="utf-8"))

    def test_the_board_file_is_regenerated(self):
        self._migrate()
        out = (self.vault / "Boards" / "Engineering.md").read_text(encoding="utf-8")
        self.assertIn(board_view.BANNER, out)
        self.assertIn(f"<!-- kanban:my-app/{self.a} -->", out)
        self.assertNotIn("@{2026-09-05}", out)

    def test_a_second_migrate_changes_nothing(self):
        self._migrate()
        board_bytes = (self.vault / "Boards" / "Engineering.md").read_bytes()
        intake_bytes = (self.vault / "Boards" / "Intake.md").read_bytes()
        backup_bytes = (self.vault / "Boards" / "Engineering.pre-generated.md").read_bytes()
        n_comments = len(self.h.comments[self.a])
        self._migrate()
        self.assertEqual((self.vault / "Boards" / "Engineering.md").read_bytes(), board_bytes)
        self.assertEqual((self.vault / "Boards" / "Intake.md").read_bytes(), intake_bytes)
        self.assertEqual((self.vault / "Boards" / "Engineering.pre-generated.md").read_bytes(), backup_bytes)
        self.assertEqual(len(self.h.comments[self.a]), n_comments)

    def test_it_pushes_both_files_in_one_commit(self):
        pushed = []

        def _git(cmd):
            pushed.append(list(cmd))
            # real git semantics: `diff --cached --quiet` exits 1 when there is
            # something staged, 0 when the index is clean - so this mock must
            # say "staged" or vault_git.push short-circuits before `commit`.
            rc = 1 if "diff" in cmd else 0
            return type("R", (), {"returncode": rc, "stdout": "", "stderr": ""})()

        self.git = _git
        self._migrate()
        commits = [" ".join(c) for c in pushed if " commit " in " ".join(c)]
        self.assertTrue(any("board: generated view (migration)" in c for c in commits))
        add = next(" ".join(c) for c in pushed if " add " in " ".join(c))
        self.assertIn("Boards/Intake.md", add)
        self.assertIn("Boards/Engineering.md", add)

    def test_no_board_file_is_a_clean_no_op(self):
        (self.vault / "Boards" / "Engineering.md").unlink()
        res = self._migrate()
        self.assertIsNone(res["backup"])
        self.assertEqual(res["annotated"], 0)


class DefaultVaultGuard(unittest.TestCase):
    """The 2026-09-07 incident guard: under AGENT_OPS_NO_SEND (the suite's own
    marker, set at import above) a call with no `vault=` must never fall
    through to the configured vault (`$OBSIDIAN_VAULT_PATH` / host.json) - it must refuse."""

    def test_without_the_marker_the_default_vault_is_used(self):
        """Production shape: no AGENT_OPS_NO_SEND, no vault= -> the module's
        default (VAULT, pinned by this test to a temp dir) is written."""
        import os, pathlib, tempfile
        from unittest import mock
        tmp = pathlib.Path(tempfile.mkdtemp())
        (tmp / "Boards").mkdir()
        with mock.patch.dict(os.environ, {"AGENT_OPS_NO_SEND": ""}), \
             mock.patch.object(board_view, "VAULT", tmp):
            os.environ.pop("AGENT_OPS_NO_SEND", None)
            board_view.write("hello")
        self.assertEqual((tmp / "Boards" / "Engineering.md").read_text(), "hello")

    def test_write_refuses_the_default_vault(self):
        with self.assertRaises(RuntimeError):
            board_view.write("x")

    def test_regenerate_refuses_the_default_vault(self):
        with self.assertRaises(RuntimeError):
            board_view.regenerate()

    def test_migrate_refuses_the_default_vault(self):
        with self.assertRaises(RuntimeError):
            board_view.migrate()

    def test_an_explicit_vault_is_never_refused(self):
        vault = Path(tempfile.mkdtemp(prefix="bv-guard-vault-"))
        (vault / "Boards").mkdir()
        p = board_view.write("x", vault=vault)
        self.assertEqual(p, vault / "Boards" / "Engineering.md")


class _CliBase(unittest.TestCase):
    def _vault(self):
        vault = Path(tempfile.mkdtemp(prefix="bv-cli-vault-"))
        (vault / "Boards").mkdir()
        return vault


class RenderCli(_CliBase):
    def test_refuses_without_vault(self):
        with self.assertRaises(SystemExit) as cm:
            board_view.main(["render"])
        self.assertEqual(cm.exception.code, 2)

    def test_runs_against_a_temp_vault(self):
        vault = self._vault()
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = board_view.main(["render", "--vault", str(vault)])
        self.assertEqual(rc, 0)
        self.assertIn(f"vault: {vault}", buf.getvalue())
        self.assertTrue((vault / "Boards" / "Engineering.md").exists())


class MigrateCli(_CliBase):
    def test_refuses_without_vault(self):
        with self.assertRaises(SystemExit) as cm:
            board_view.main(["migrate"])
        self.assertEqual(cm.exception.code, 2)

    def test_runs_against_a_temp_vault(self):
        vault = self._vault()
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = board_view.main(["migrate", "--vault", str(vault)])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn(f"vault: {vault}", out)
        self.assertIn("backup:", out)
