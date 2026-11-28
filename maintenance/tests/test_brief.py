# mutation-check: targets-predate-range b957739c9 -- fixture rename only, no behaviour pinned ::
#   Cli.test_vault_copy_through_main_refuses_under_test_and_stays_fail_open
import datetime
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("AGENT_OPS_NO_SEND", "1")

import brief  # noqa: E402

NOW = datetime.datetime(2026, 9, 11, 8, 30).astimezone()
from capsule.tests.public_tree import private_only  # noqa: E402


def _ev(ts, kind, **kw):
    row = {"ts": ts, "kind": kind, "source": "test", "text": "", "payload": {}}
    row.update(kw)
    return row



class Fixture(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="brief-"))
        self.state = self.dir / "state"
        (self.state / "jobs").mkdir(parents=True)
        self.spool = self.state / "events.jsonl"
        self.spool.write_text("")

    def write_events(self, rows):
        self.spool.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    def write_job(self, job, *, end, exit_=0, skipped=None):
        (self.state / "jobs" / f"{job}.json").write_text(json.dumps({
            "job": job, "start": end, "end": end, "duration_s": 1,
            "exit": exit_, "skipped": skipped, "commit": None, "log": ""}))


class Waiting(Fixture):
    def test_empty_reads_zero(self):
        self.assertEqual(brief.waiting_block(blocked=[], seen_path=self.state / "notify-seen.json",
                                             now=NOW),
                         ["WAITING ON YOU - 0"])

    def test_oldest_first_with_an_age(self):
        blocked = [
            {"board": "capsule-corp", "task_id": "t_new", "title": "New one",
             "reason": "DECISION: pick a store", "opened_ts": NOW.timestamp() - 3 * 3600},
            {"board": "sample-app", "task_id": "t_old", "title": "Old one",
             "reason": "DECISION: retry policy", "opened_ts": NOW.timestamp() - 26 * 3600},
        ]
        lines = brief.waiting_block(blocked=blocked, seen_path=self.state / "notify-seen.json",
                                    now=NOW)
        self.assertEqual(lines[0], "WAITING ON YOU - 2")
        self.assertEqual(lines[1], "  * Decision - sample-app/t_old - Old one (open 26 h)")
        self.assertEqual(lines[2], "  * Decision - capsule-corp/t_new - New one (open 3 h)")

    def test_the_age_of_a_real_decision_comes_from_its_decision_open_event(self):
        # blocked_decisions() carries no timestamp (spec section 4), so the age
        # is looked up from the events spool's decision_open row.
        self.write_events([_ev(NOW.timestamp() - 14 * 3600, "card", task_id="t_old",
                               payload={"event": "decision_open", "state": "open"})])
        blocked = [{"board": "capsule-corp", "task_id": "t_old", "title": "Model routing",
                    "reason": "DECISION: model routing"}]
        lines = brief.waiting_block(blocked=blocked, state_dir=self.state,
                                    seen_path=self.state / "notify-seen.json", now=NOW)
        self.assertEqual(lines[1], "  * Decision - capsule-corp/t_old - Model routing (open 14 h)")

    def test_a_decision_open_event_older_than_30_days_before_now_is_not_used(self):
        # The window ends at the caller's `now`: 29 days back counts, 31 does not.
        for days, expect in ((29, "(open 696 h)"), (31, "(age unknown)")):
            self.write_events([_ev(NOW.timestamp() - days * 86400, "card", task_id="t",
                                   payload={"event": "decision_open", "state": "open"})])
            blocked = [{"board": "b", "task_id": "t", "title": "T", "reason": "DECISION: x"}]
            lines = brief.waiting_block(blocked=blocked, state_dir=self.state,
                                        seen_path=self.state / "notify-seen.json", now=NOW)
            self.assertEqual(lines[1], f"  * Decision - b/t - T {expect}")

    def test_an_unknown_age_says_so_rather_than_claiming_zero(self):
        blocked = [{"board": "b", "task_id": "t", "title": "T", "reason": "DECISION: x"}]
        lines = brief.waiting_block(blocked=blocked, state_dir=self.state,
                                    seen_path=self.state / "notify-seen.json", now=NOW)
        self.assertEqual(lines[1], "  * Decision - b/t - T (age unknown)")

    def test_the_real_notify_seen_keys_are_classified(self):
        seen = self.state / "notify-seen.json"
        seen.write_text(json.dumps({
            "t_pr:pr": "2026-09-10",                      # PR ready - waiting
            "t_b:blocked:0123456789": "2026-09-10",       # stuck - waiting
            "spec:retention-sweep:notified": "2026-09-10",  # spec ready - waiting
            "t_h:huddle": "2026-09-10",                   # narration - NOT waiting
            "t_d:done": "2026-09-10",                     # narration - NOT waiting
            "budget:t_x:park": "2026-09-10",              # C2's own key - NOT waiting
        }))
        vault = self.dir / "vault"
        (vault / "Specs").mkdir(parents=True)
        (vault / "Specs" / "retention-sweep.md").write_text("# retention sweep\n")
        lines = brief.waiting_block(blocked=[], seen_path=seen, now=NOW, vault=vault)
        self.assertEqual(lines[0], "WAITING ON YOU - 3")
        body = "\n".join(lines[1:])
        self.assertIn("PR - t_pr", body)
        self.assertIn("Stuck - t_b", body)
        self.assertIn("Spec - retention-sweep", body)
        self.assertNotIn("t_h", body)
        self.assertNotIn("t_d", body)
        self.assertNotIn("t_x", body)
        self.assertIn("(open 32 h)", body)   # stored date 09-10, now 09-11 08:30

    def test_a_raising_source_degrades_to_one_honest_line(self):
        def boom():
            raise RuntimeError("kanban unreachable")
        lines = brief.waiting_block(blocked=boom, seen_path=self.state / "notify-seen.json",
                                    now=NOW)
        self.assertEqual(lines, ["WAITING ON YOU - ?  (board unreadable)"])

    def test_state_dir_alone_is_enough_to_find_notify_seen(self):
        # C2 (Task 4 review, load-bearing): waiting_items() used to resolve
        # seen_path straight to the module-level STATE (the real
        # maintenance/state/notify-seen.json) whenever seen_path itself was
        # omitted, ignoring state_dir entirely. --state-dir is the only
        # isolation lever Tasks 5/6's shell suites have, so passing only
        # state_dir= (no seen_path=) must read THIS scratch dir's
        # notify-seen.json, not the real one.
        (self.state / "notify-seen.json").write_text(json.dumps(
            {"t_c2:pr": "2026-09-10"}))
        lines = brief.waiting_block(blocked=[], state_dir=self.state, now=NOW)
        self.assertEqual(lines[0], "WAITING ON YOU - 1")
        self.assertIn("PR - t_c2", lines[1])


class Overnight(Fixture):
    def test_three_lines_from_state(self):
        ts = NOW.timestamp() - 3600
        self.write_events([
            _ev(ts, "card", title="retention sweep", payload={"event": "merged", "state": "closed"}),
            _ev(ts, "job", payload={"job": "nightly", "exit": 0}),
            _ev(ts, "job", payload={"job": "inbox", "exit": 142}),
        ])
        self.write_job("nightly", end=(NOW - datetime.timedelta(hours=9)).isoformat())
        self.write_job("inbox", end=(NOW - datetime.timedelta(hours=2)).isoformat(), exit_=142)
        (self.state / "heal.json").write_text(json.dumps({"docker": {"fails": 0}}))
        lines = brief.overnight_block(state_dir=self.state, now=NOW)
        self.assertEqual(lines[0], "OVERNIGHT")
        self.assertIn("1 merged", lines[1])
        self.assertIn("inbox", lines[2])
        self.assertIn("heal ok", lines[3])

    def test_a_missing_state_dir_reads_as_an_empty_night(self):
        lines = brief.overnight_block(state_dir=self.dir / "nope", now=NOW)
        self.assertEqual(lines[1], "  0 card(s) done - 0 merged - 0 needs repair")
        self.assertEqual(lines[2], "  jobs 0/0 ok, 0 failed")

    def test_a_raising_reader_degrades_to_one_line(self):
        orig = brief.events.read

        def boom(*a, **k):
            raise RuntimeError("spool unreadable")

        brief.events.read = boom
        try:
            self.assertEqual(brief.overnight_block(state_dir=self.state, now=NOW),
                             ["OVERNIGHT", "  overnight: unavailable"])
        finally:
            brief.events.read = orig


class Budget(Fixture):
    def test_window_week_and_a_consequence(self):
        self.write_events([_ev(NOW.timestamp() - 600, "usage", payload={
            "session_pct": 34.0, "week_pct": 61.0, "runs_today": 0, "ceiling": 12})])
        lines = brief.budget_block(state_dir=self.state, now=NOW)
        self.assertEqual(lines[0], "BUDGET")
        self.assertEqual(lines[1], "  5-h window 34% - week 61% - runs today 0/12")
        self.assertEqual(lines[2], "  -> heavy work fine")

    def test_a_pause_is_the_consequence(self):
        self.write_events([_ev(NOW.timestamp() - 600, "usage", payload={
            "session_pct": 86.0, "week_pct": 61.0, "runs_today": 9, "ceiling": 12})])
        (self.state / "usage-guard.json").write_text(json.dumps(
            {"paused_until": NOW.timestamp() + 3600, "announced": True}))
        lines = brief.budget_block(state_dir=self.state, now=NOW)
        self.assertIn("agents paused until", lines[2])
        # F4 (row 208): a bare %H:%M crossing midnight reads as already
        # past - the weekday must be in the consequence line too.
        self.assertIn(NOW.strftime("%a"), lines[2])

    def test_absent_when_there_is_no_usage_row(self):
        self.assertEqual(brief.budget_block(state_dir=self.state, now=NOW), [])


class Parked(Fixture):
    def test_counts_and_names_up_to_three(self):
        cards = [{"board": "b", "task_id": f"t_{i}", "reason": "NEEDS REPAIR: budget: cost"}
                 for i in range(4)]
        lines = brief.parked_block(cards=cards)
        self.assertEqual(lines[0], "PARKED - 4")
        self.assertEqual(len(lines), 4)
        self.assertIn("b/t_0", lines[1])

    def test_absent_when_nothing_is_parked(self):
        self.assertEqual(brief.parked_block(cards=[]), [])


class Header(unittest.TestCase):
    def test_shape(self):
        self.assertTrue(brief.header(now=NOW, at="08:30").startswith(
            "== Capsule - Fri 11 Sep - 08:30 "))


class BriefText(Fixture):
    def _text(self, **kw):
        self.write_events([_ev(NOW.timestamp() - 600, "usage", payload={
            "session_pct": 34.0, "week_pct": 61.0, "runs_today": 0, "ceiling": 12})])
        (self.state / "heal.json").write_text(json.dumps({"docker": {"fails": 0}}))
        args = dict(state_dir=self.state, blocked=[], cards=[],
                    seen_path=self.state / "notify-seen.json",
                    deferred=[], budget=None, extra=[], now=NOW)
        args.update(kw)
        return brief.brief_text(**args)

    def test_the_shape(self):
        text = self._text()
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("== Capsule - Fri 11 Sep - 08:30"))
        self.assertEqual(lines[1], "")
        self.assertEqual(lines[2], "WAITING ON YOU - 0")
        self.assertIn("OVERNIGHT", text)
        self.assertIn("BUDGET", text)
        self.assertTrue(text.rstrip().endswith("Board: Boards/Engineering.md"))
        self.assertIn("Nothing else needs you today.", text)

    def test_the_deferred_batch_and_the_budget_line_are_appended(self):
        text = self._text(deferred=["waiting: capsule-corp/t_9 - DECISION - vector store"],
                          budget="budget: 1 actionable item(s) waiting - the cap is 3/day")
        self.assertIn("waiting: capsule-corp/t_9 - DECISION - vector store", text)
        self.assertIn("budget: 1 actionable item(s) waiting", text)

    def test_extra_lines_from_report_sh_are_kept_verbatim(self):
        text = self._text(extra=["power: 3 merged (24h), median 1 tokens", "## Notices",
                                 "notices: none"])
        self.assertIn("power: 3 merged (24h), median 1 tokens", text)
        self.assertIn("## Notices\nnotices: none", text)

    def test_all_blank_extra_lines_add_no_trailing_blank(self):
        # report.sh's `printf '%s\n' "$POWER_LINES" "$RETRIEVAL_LINES" ...`
        # produces exactly five empty strings on a quiet day when none of
        # those readers fired - a list of blanks, not an empty list. `extra`
        # being truthy on that raw list alone used to add a spurious
        # trailing blank line (fix round 1, review R5-6): assert the
        # closing block follows PARKED/BUDGET with no extra blank between.
        with_blanks = self._text(extra=["", "", "", "", ""])
        without = self._text(extra=[])
        self.assertEqual(with_blanks, without)

    def test_a_v2_brief_always_carries_the_shape_guard_string(self):
        self.assertIn("== Capsule", self._text())


class VaultCopy(Fixture):
    def test_path_and_frontmatter(self):
        vault = self.dir / "vault"
        (vault / "Knowledge").mkdir(parents=True)
        note = brief.vault_note("== Capsule - Fri 11 Sep - 08:30 ==\n\nWAITING ON YOU - 0",
                                now=NOW)
        self.assertTrue(note.startswith("---\n"))
        self.assertIn("type: brief", note)
        self.assertIn("date: 2026-09-11", note)
        self.assertIn("source: maintenance/brief.py", note)
        self.assertIn("## For future agent", note)
        self.assertIn("WAITING ON YOU - 0", note)
        self.assertEqual(brief.vault_copy_path(now=NOW, vault=vault).name,
                         "Morning report 2026-09-11.md")

    def test_job_label_is_read_from_the_template_file_name(self):
        root = self.dir / "tree"
        (root / "launchd" / "templates").mkdir(parents=True)
        (root / "maintenance").mkdir()
        (root / "launchd" / "templates" / "x.y.obsidian-report.plist").touch()
        with patch.object(brief, "MAINT", root / "maintenance"):
            note = brief.vault_note("b", now=NOW)
            self.assertIn("job: report (08:30, x.y.obsidian-report)", note)
        with patch.object(brief, "MAINT", root / "nowhere" / "maintenance"):
            self.assertIn("job: report (08:30, obsidian-report)", brief.vault_note("b", now=NOW))

    def test_write_is_idempotent_and_pushes_one_path(self):
        vault = self.dir / "vault"
        (vault / "Knowledge").mkdir(parents=True)
        pushed = []

        def runner(args, **kw):
            pushed.append(list(args))
            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()

        rel1 = brief.write_vault_copy("body one", vault=vault, now=NOW, runner=runner)
        rel2 = brief.write_vault_copy("body two", vault=vault, now=NOW, runner=runner)
        self.assertEqual(rel1, "Knowledge/Morning report 2026-09-11.md")
        self.assertEqual(rel1, rel2)
        self.assertIn("body two", (vault / rel1).read_text())
        self.assertEqual(len(list((vault / "Knowledge").iterdir())), 1)
        self.assertTrue(any(rel1 in " ".join(a) for a in pushed))

    def test_pushes_exactly_the_one_path_it_wrote_and_nothing_else(self):
        # Ruling 6: no test may let vault_git.push shell out unmocked, and this
        # asserts the push touches exactly the one path write_vault_copy wrote.
        vault = self.dir / "vault"
        (vault / "Knowledge").mkdir(parents=True)
        pushed = []

        def runner(args, **kw):
            pushed.append(list(args))
            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()

        rel = brief.write_vault_copy("body", vault=vault, now=NOW, runner=runner)
        md_paths = {a for call in pushed for a in call if str(a).endswith(".md")}
        self.assertEqual(md_paths, {rel})

    def test_a_write_failure_is_survivable(self):
        self.assertIsNone(brief.write_vault_copy("x", vault=Path("/proc/nope"), now=NOW,
                                                 runner=lambda *a, **k: None))


class VaultCopyGuard(Fixture):
    """Ruling 10: brief.VAULT is computed at import time and falls back to the
    the real notes vault when OBSIDIAN_VAULT_PATH is unset. write_vault_copy must
    never resolve to that real vault under test - it refuses outright when no
    explicit vault= is given and OBSIDIAN_VAULT_PATH is unset."""

    def setUp(self):
        super().setUp()
        self._orig_vault_path = os.environ.get("OBSIDIAN_VAULT_PATH")
        self._orig_no_send = os.environ.get("AGENT_OPS_NO_SEND")
        os.environ.pop("OBSIDIAN_VAULT_PATH", None)
        os.environ["AGENT_OPS_NO_SEND"] = "1"

    def tearDown(self):
        if self._orig_vault_path is None:
            os.environ.pop("OBSIDIAN_VAULT_PATH", None)
        else:
            os.environ["OBSIDIAN_VAULT_PATH"] = self._orig_vault_path
        if self._orig_no_send is None:
            os.environ.pop("AGENT_OPS_NO_SEND", None)
        else:
            os.environ["AGENT_OPS_NO_SEND"] = self._orig_no_send
        super().tearDown()

    def test_refuses_the_default_vault_under_test(self):
        self.assertIsNone(brief.write_vault_copy("x", now=NOW))

    def test_refuses_even_when_OBSIDIAN_VAULT_PATH_is_set(self):
        # Ruling 16: the owner's own shell exports OBSIDIAN_VAULT_PATH, so a
        # guard that only fires when it is UNSET would be inert exactly where
        # it matters. The guard must fire on AGENT_OPS_NO_SEND alone.
        os.environ["OBSIDIAN_VAULT_PATH"] = str(self.dir / "not-the-real-vault")
        self.assertIsNone(brief.write_vault_copy("x", now=NOW))


class Cli(Fixture):
    def test_brief_prints_and_exits_zero(self):
        rc = brief.main(["--brief", "--state-dir", str(self.state), "--no-board",
                         "--no-batch"])
        self.assertEqual(rc, 0)

    def test_vault_copy_through_main_refuses_under_test_and_stays_fail_open(self):
        # C1 (review round 1): main() used to default --vault to str(VAULT),
        # so it never passed None and the Ruling 10/16 guard in
        # write_vault_copy never fired - the reviewer demonstrated this
        # writing into the real vault path under AGENT_OPS_NO_SEND=1. With no
        # --vault given, main() must pass vault=None through, the guard must
        # fire, and the CLI must still exit 0 (fail-open: the brief already
        # sent, the vault copy is best-effort).
        src = self.state / "brief-body.txt"
        src.write_text("== Capsule - Fri 11 Sep - 08:30 ==\n\nWAITING ON YOU - 0")
        self._orig_no_send = os.environ.get("AGENT_OPS_NO_SEND")
        os.environ["AGENT_OPS_NO_SEND"] = "1"
        try:
            rc = brief.main(["--vault-copy", "--file", str(src)])
        finally:
            if self._orig_no_send is None:
                os.environ.pop("AGENT_OPS_NO_SEND", None)
            else:
                os.environ["AGENT_OPS_NO_SEND"] = self._orig_no_send
        self.assertEqual(rc, 0)
        configured = os.environ.get("OBSIDIAN_VAULT_PATH") or brief.VAULT
        if configured:    # no vault configured means nothing could have been written
            self.assertFalse((Path(configured) / brief.VAULT_SUBDIR
                              / f"Morning report {NOW.date().isoformat()}.md").exists())


class Close(Fixture):
    def test_five_blocks_and_the_quiet_contract(self):
        midnight = NOW.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        self.write_events([
            _ev(midnight + 3600, "card", title="a", payload={"event": "merged", "state": "closed"}),
            _ev(midnight + 3700, "card", title="b", payload={"event": "done", "state": "closed"}),
            _ev(midnight + 3800, "card", payload={"event": "decision_answered"}),
            _ev(midnight + 3900, "usage", payload={"session_pct": 20.0, "week_pct": 61.0,
                                                   "runs_today": 4, "ceiling": 12}),
        ])
        text = brief.close_text(state_dir=self.state, blocked=[], cards=[],
                                seen_path=self.state / "notify-seen.json",
                                now=NOW.replace(hour=18, minute=0))
        self.assertTrue(text.splitlines()[0].startswith("== Capsule - Fri 11 Sep - 18:00"))
        self.assertIn("TODAY", text)
        self.assertIn("2 card(s) done - 1 merged", text)
        self.assertIn("1 decision answered - 0 page(s)", text)
        self.assertIn("TONIGHT", text)
        self.assertIn("nightly 23:00", text)
        self.assertIn("TOMORROW", text)
        self.assertIn("nothing - the night can run", text)
        self.assertIn("week 61%", text)
        self.assertTrue(text.rstrip().endswith("Quiet until 07:30 unless something pages."))

    def test_tomorrow_names_the_oldest_waiting_item(self):
        close_now = NOW.replace(hour=18, minute=0)
        blocked = [{"board": "capsule-corp", "task_id": "t_old", "title": "Model routing",
                    "reason": "DECISION: model routing",
                    "opened_ts": close_now.timestamp() - 9 * 3600}]
        text = brief.close_text(state_dir=self.state, blocked=blocked, cards=[],
                                seen_path=self.state / "notify-seen.json",
                                now=close_now)
        self.assertIn("first item: Decision - capsule-corp/t_old - Model routing (open 9 h)", text)

    def test_a_missing_spool_still_composes(self):
        text = brief.close_text(state_dir=self.dir / "nope", blocked=[], cards=[], now=NOW)
        self.assertIn("== Capsule", text)
        self.assertIn("Quiet until 07:30", text)


class Cadence(unittest.TestCase):
    @private_only
    def test_the_cadence_map_matches_the_installed_plists(self):
        # Ruling 9: brief.py's comment claims "tests/test_brief.py asserts it
        # [CADENCE_TONIGHT] matches launchd/templates/*.plist" - make that
        # true. Every (job, "HH:MM") in CADENCE_TONIGHT must match the
        # Hour/Minute StartCalendarInterval of the plist whose ProgramArguments
        # ends with that job name (the run.sh <job> convention every script
        # and Claude job template shares).
        import plistlib

        repo = Path(__file__).resolve().parents[2]
        templates = repo / "launchd" / "templates"
        by_job = {}
        for p in sorted(templates.glob("*.plist")):
            with open(p, "rb") as fh:
                d = plistlib.load(fh)
            args = d.get("ProgramArguments") or []
            sci = d.get("StartCalendarInterval") or {}
            if not args or "Hour" not in sci or "Minute" not in sci:
                continue
            by_job[args[-1]] = (sci["Hour"], sci["Minute"])

        for job, at in brief.CADENCE_TONIGHT:
            self.assertIn(job, by_job, f"no installed plist runs job {job!r}")
            hour, minute = (int(x) for x in at.split(":"))
            self.assertEqual(by_job[job], (hour, minute),
                              f"CADENCE_TONIGHT says {job} at {at}, "
                              f"the plist says {by_job[job]}")


if __name__ == "__main__":
    unittest.main()


class WaitingDedupe(Fixture):
    def test_one_line_per_card_and_kind(self):
        seen = {f"t_660ac0be:blocked:{h}": (NOW - datetime.timedelta(hours=h)).isoformat()
                for h in (1, 2, 3)}
        seen["t_1:pr:9"] = (NOW - datetime.timedelta(hours=5)).isoformat()
        (self.state / "notify-seen.json").write_text(json.dumps(seen))
        items = brief.waiting_items(blocked=[], seen_path=self.state / "notify-seen.json",
                                    now=NOW, state_dir=self.state)
        stuck = [i for i in items if i["kind"] == "Stuck"]
        self.assertEqual(len(stuck), 1)
        self.assertEqual(stuck[0]["what"], "t_660ac0be")
        self.assertEqual(stuck[0]["age_h"], max(i["age_h"] for i in stuck) )
        self.assertEqual(sum(1 for i in items if i["kind"] == "PR"), 1)


class WaitingBrainstormKeys(Fixture):
    def _items(self, rows, blocked=()):
        batch = self.dir / "batch.json"
        batch.write_text(json.dumps(rows))
        return brief.waiting_items(blocked=list(blocked), seen_path=self.state / "notify-seen.json",
                                   now=NOW, state_dir=self.state, active_boards=[],
                                   batch_path=batch, home=self.dir / "home")

    def _row(self, bid, title):
        return {"kind": "waiting", "task_id": bid, "text": f"Brainstorm - {title}",
                "ts": NOW.timestamp() - 3600}

    def test_same_title_different_ids_both_appear(self):
        items = self._items([self._row("a" * 16, "Plan X"), self._row("b" * 16, "Plan X")])
        self.assertEqual([i["kind"] for i in items].count("Brainstorm"), 2)

    def test_a_brainstorm_titled_like_a_card_does_not_hide_a_decision(self):
        blocked = [{"board": "board", "task_id": "t_x", "title": "Pick",
                    "reason": "DECISION: pick"}]
        items = self._items([self._row("a" * 16, "board/t_x")], blocked)
        self.assertEqual(sorted(i["kind"] for i in items), ["Brainstorm", "Decision"])


class WaitingBoardTruth(Fixture):
    """F1 (row 208): every notify-seen row must be cross-checked against
    board truth before it is trusted."""

    def _seen(self, mapping):
        path = self.state / "notify-seen.json"
        path.write_text(json.dumps(mapping))
        return path

    def test_a_done_card_is_dropped(self):
        seen = self._seen({"t_1:pr": "2026-09-10"})
        items = brief.waiting_items(
            blocked=[], seen_path=seen, now=NOW, state_dir=self.state,
            card_status=lambda b, t: "done", active_boards={"capsule-corp"})
        self.assertEqual(items, [])

    def test_an_archived_card_is_dropped(self):
        seen = self._seen({"t_2:blocked:0123456789": "2026-09-10"})
        items = brief.waiting_items(
            blocked=[], seen_path=seen, now=NOW, state_dir=self.state,
            card_status=lambda b, t: "archived", active_boards={"capsule-corp"})
        self.assertEqual(items, [])

    def test_an_unknown_status_card_is_kept(self):
        def boom(b, t):
            raise RuntimeError("kanban unreachable")
        seen = self._seen({"t_3:blocked:0123456789": "2026-09-10"})
        items = brief.waiting_items(
            blocked=[], seen_path=seen, now=NOW, state_dir=self.state,
            card_status=boom, active_boards={"capsule-corp"})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["what"], "t_3")

    def test_a_spec_row_with_no_note_is_dropped(self):
        seen = self._seen({"spec:one:decision-wait": "2026-09-10"})
        vault = self.dir / "vault"   # no Specs/one.md written under it
        items = brief.waiting_items(blocked=[], seen_path=seen, now=NOW,
                                    state_dir=self.state, vault=vault)
        self.assertEqual(items, [])

    def test_an_inactive_board_row_is_dropped(self):
        import board

        def not_here(b, t):
            raise board.CardNotFound(f"{b}: no card {t}")
        seen = self._seen({"t_4:gate:0123456789": "2026-09-10"})
        items = brief.waiting_items(
            blocked=[], seen_path=seen, now=NOW, state_dir=self.state,
            card_status=not_here, active_boards={"capsule-corp", "sample-app"})
        self.assertEqual(items, [])

    def test_two_kinds_for_one_card_collapse_to_one_line(self):
        seen = self._seen({
            "t_5:pr": "2026-09-10",
            "t_5:blocked:0123456789": "2026-09-09",
        })
        items = brief.waiting_items(blocked=[], seen_path=seen, now=NOW,
                                    state_dir=self.state)
        self.assertEqual([i["what"] for i in items], ["t_5"])

    def test_the_cap_prints_and_n_more(self):
        seen = self._seen({f"t_{i}:pr": "2026-09-10" for i in range(10)})
        lines = brief.waiting_block(blocked=[], seen_path=seen, now=NOW,
                                    state_dir=self.state)
        self.assertEqual(lines[0], "WAITING ON YOU - 10")
        self.assertEqual(lines[-1], "  ... and 2 more")
        self.assertEqual(len(lines), 1 + brief.WAITING_CAP + 1)


class DefaultCardStatus(unittest.TestCase):
    """The live status was a legacy CLI call; with it gone the default
    lookup answers "unknown" and never touches a board CLI."""

    def test_default_lookup_is_unknown_and_never_opens_a_board(self):
        import board as board_mod
        def boom(*a, **k):
            raise AssertionError("the default status lookup must not open a board")
        with patch.dict(os.environ, {}), patch.object(board_mod, "open_board", boom):
            os.environ.pop("AGENT_OPS_NO_SEND", None)
            self.assertEqual(brief._default_card_status("capsule-corp", "t_x"), "unknown")

    def test_resolver_moves_to_the_next_board_after_no_such_task(self):
        import board as board_mod
        calls = []
        def cs(name, tid):
            calls.append(name)
            if name == "capsule-corp":
                raise board_mod.CardNotFound("capsule-corp: no card")
            return "done"
        boards = ["sample-app", "capsule-corp"]
        self.assertEqual(brief._resolve_card_status("t_x", cs, boards), "done")
        self.assertEqual(calls, ["capsule-corp", "sample-app"])
