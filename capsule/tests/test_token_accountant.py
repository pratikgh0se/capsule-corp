"""Token accountant (row 558, card t_ede356cf): `python3 -m capsule tokens`. Fixture transcripts are
hand-built synthetic JSONL in a temp dir. Two oracles (Z2): the grand total equals the sum of the
per-card rows, and equals the sum of the per-role and per-outcome rows; the hand-computed table
for a fixed fixture is the second."""
import hashlib
import io
import json
import os
import tempfile
import unittest
import datetime
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from capsule import __main__ as cli
from capsule import intent, paths, token_accountant

DAY = "2026-10-01"


def usage(fresh_in=0, out=0, cc=0, cr=0):
    return {"input_tokens": fresh_in, "output_tokens": out, "cache_creation_input_tokens": cc,
            "cache_read_input_tokens": cr}


class Tree:
    """A synthetic ~/.claude/projects: one project dir, one session, any number of subagents."""

    def __init__(self, root):
        self.root = Path(root)
        self.proj = self.root / "-proj"
        self.session = "sess1"
        (self.proj / self.session / "subagents").mkdir(parents=True)
        self.notes = []

    def agent(self, aid, *, description, prompt, tokens, day=DAY, status="completed", notified=None):
        sub = self.proj / self.session / "subagents"
        (sub / f"agent-{aid}.meta.json").write_text(json.dumps(
            {"agentType": "general-purpose", "description": description, "toolUseId": f"toolu_{aid}"}))
        lines = [{"type": "user", "timestamp": f"{day}T10:00:00.000Z", "message": {"role": "user", "content": prompt}},
                 {"type": "assistant", "timestamp": f"{day}T10:00:05.000Z",
                  "message": {"role": "assistant", "usage": tokens}}]
        (sub / f"agent-{aid}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
        if notified is not None:
            body = (f"<task-notification>\n<task-id>x</task-id>\n<tool-use-id>toolu_{aid}</tool-use-id>\n"
                    f"<status>{status}</status>\n<usage><subagent_tokens>{notified}</subagent_tokens>"
                    f"<tool_uses>3</tool_uses></usage>\n</task-notification>")
            self.notes.append({"type": "queue-operation", "operation": "enqueue", "content": body,
                               "timestamp": f"{day}T11:00:00.000Z"})

    def finish(self):
        (self.proj / f"{self.session}.jsonl").write_text("".join(json.dumps(n) + "\n" for n in self.notes))
        return self.root


def sample(root):
    """card A: implementer 100 + reviewer 50 (landed); card B: implementer 10, 10 (second redoes the
    first), card C: implementer 20 that failed; card D: keeper 30; one unattributed 5 on another day."""
    t = Tree(root)
    t.agent("a1", description="implementer for t_aaaaaaaa", prompt="Work the card.", tokens=usage(40, 10, 50, cr=7), notified=900)
    t.agent("a2", description="review", prompt="You are the reviewer. Card t_aaaaaaaa.", tokens=usage(10, 10, 30), notified=400)
    t.agent("b1", description="implementer t_bbbbbbbb", prompt="x", tokens=usage(0, 5, 5))
    t.agent("b2", description="implementer t_bbbbbbbb round 2", prompt="x", tokens=usage(0, 5, 5), notified=10)
    t.agent("c1", description="implementer t_cccccccc", prompt="x", tokens=usage(5, 5, 15), status="failed", notified=20)
    t.agent("d1", description="keeper t_dddddddd close", prompt="x", tokens=usage(10, 10, 10))
    t.agent("u1", description="misc", prompt="no card here", tokens=usage(5), day="2026-09-30")
    return t.finish()


class Scan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = sample(self.tmp.name)
        self.runs = token_accountant.scan(self.root, DAY)

    def by_agent(self):
        return {r["agent"]: r for r in self.runs}

    def test_day_filter_drops_other_days(self):
        self.assertEqual(sorted(self.by_agent()), ["a1", "a2", "b1", "b2", "c1", "d1"])

    def test_spend_is_input_plus_output_plus_cache_creation_not_cache_reads(self):
        t = Tree(tempfile.mkdtemp(dir=self.tmp.name))
        t.agent("z", description="implementer t_12345678", prompt="x", tokens=usage(1, 2, 4, cr=1000))
        (run,) = token_accountant.scan(t.finish(), DAY)
        self.assertEqual(run["tokens"], 7)

    def test_card_comes_from_description_then_prompt_else_unattributed(self):
        runs = self.by_agent()
        self.assertEqual(runs["a1"]["card"], "t_aaaaaaaa")
        self.assertEqual(runs["a2"]["card"], "t_aaaaaaaa")    # only the prompt names it
        t = Tree(tempfile.mkdtemp(dir=self.tmp.name))
        t.agent("q", description="misc", prompt="nothing", tokens=usage(1))
        t.agent("r", description="t_0000000a here", prompt="and t_0000000b there", tokens=usage(1))
        got = {r["agent"]: r["card"] for r in token_accountant.scan(t.finish(), DAY)}
        self.assertEqual(got, {"q": "unattributed", "r": "t_0000000a"})

    def test_role_from_description_then_prompt(self):
        runs = self.by_agent()
        self.assertEqual([runs[a]["role"] for a in ("a1", "a2", "d1")], ["implementer", "reviewer", "keeper"])

    def test_outcome_failed_redone_landed(self):
        runs = self.by_agent()
        self.assertEqual(runs["d1"]["outcome"], "unknown")     # no notification: crashed, hung or still running
        self.assertEqual(runs["c1"]["outcome"], "failed")
        self.assertEqual(runs["b1"]["outcome"], "redone")      # b2 is a later run of the same card and role
        self.assertEqual(runs["b2"]["outcome"], "landed")
        self.assertEqual(runs["a1"]["outcome"], "landed")

    def test_a_later_run_marks_an_earlier_unknown_run_redone(self):
        t = Tree(tempfile.mkdtemp(dir=self.tmp.name))
        for aid in ("e1", "e2"):
            t.agent(aid, description="keeper t_33333333", prompt="x", tokens=usage(1))
        got = {r["agent"]: r["outcome"] for r in token_accountant.scan(t.finish(), DAY)}
        self.assertEqual(got, {"e1": "redone", "e2": "unknown"})

    def test_description_names_the_role_before_the_prompt(self):
        t = Tree(tempfile.mkdtemp(dir=self.tmp.name))
        t.agent("p", description="reviewer t_44444444", prompt="You are the implementer.", tokens=usage(1))
        (run,) = token_accountant.scan(t.finish(), DAY)
        self.assertEqual(run["role"], "reviewer")

    def test_old_session_files_are_not_read_for_notifications(self):
        t = Tree(tempfile.mkdtemp(dir=self.tmp.name))
        t.agent("o", description="implementer t_55555555", prompt="x", tokens=usage(1), status="failed", notified=1)
        root = t.finish()
        old = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc).timestamp()
        os.utime(t.proj / "sess1.jsonl", (old, old))
        self.assertEqual(token_accountant.scan(root, DAY)[0]["outcome"], "unknown")
        new = datetime.datetime(2026, 10, 2, tzinfo=datetime.timezone.utc).timestamp()
        os.utime(t.proj / "sess1.jsonl", (new, new))
        self.assertEqual(token_accountant.scan(root, DAY)[0]["outcome"], "failed")

    def test_two_notifications_on_one_line_never_cross_pair(self):
        t = Tree(tempfile.mkdtemp(dir=self.tmp.name))
        t.agent("m1", description="implementer t_66666666", prompt="x", tokens=usage(1))
        t.agent("m2", description="implementer t_77777777", prompt="x", tokens=usage(1))
        one = lambda aid, st: (f"<task-notification><tool-use-id>toolu_{aid}</tool-use-id><status>{st}</status>"
                               f"<usage><subagent_tokens>5</subagent_tokens></usage></task-notification>")
        t.notes.append({"type": "user", "content": one("m1", "completed") + " and " + one("m2", "failed")})
        got = {r["agent"]: r["outcome"] for r in token_accountant.scan(t.finish(), DAY)}
        self.assertEqual(got, {"m1": "landed", "m2": "failed"})

    def test_a_symlinked_project_dir_is_not_followed_out_of_the_root(self):
        outside = Tree(tempfile.mkdtemp(dir=self.tmp.name))
        outside.agent("x", description="implementer t_99999999", prompt="x", tokens=usage(7))
        outside.finish()
        t = Tree(tempfile.mkdtemp(dir=self.tmp.name))
        (t.root / "-link").symlink_to(outside.proj, target_is_directory=True)
        (t.proj / "sess1" / "subagents" / "agent-f.jsonl").symlink_to(
            outside.proj / "sess1" / "subagents" / "agent-x.jsonl")
        self.assertEqual(token_accountant.scan(t.finish(), DAY), [])

    def test_unreadable_lines_and_empty_meta_count_zero_tokens_not_a_crash(self):
        t = Tree(tempfile.mkdtemp(dir=self.tmp.name))
        sub = t.proj / "sess1" / "subagents"
        (sub / "agent-bad.jsonl").write_text('not json\n{"type": "assistant"}\n'
                                             '{"type": "user", "timestamp": "' + DAY + 'T01:00:00Z"}\n')
        (sub / "agent-bad.meta.json").write_text("{}")
        (run,) = token_accountant.scan(t.finish(), DAY)
        self.assertEqual((run["agent"], run["tokens"], run["card"]), ("bad", 0, "unattributed"))


class Report(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rep = token_accountant.report(token_accountant.scan(sample(self.tmp.name), DAY))

    def test_oracle_one_hand_computed_table(self):
        cards = {c["card"]: c["tokens"] for c in self.rep["cards"]}
        self.assertEqual(cards, {"t_aaaaaaaa": 150, "t_bbbbbbbb": 20, "t_cccccccc": 25, "t_dddddddd": 30})
        self.assertEqual(self.rep["roles"], {"implementer": 145, "reviewer": 50, "keeper": 30})
        self.assertEqual(self.rep["outcomes"], {"landed": 160, "failed": 25, "redone": 10, "unknown": 30})
        self.assertEqual(self.rep["grand_total"], 225)

    def test_oracle_two_grand_total_equals_sum_of_every_breakdown(self):
        total = self.rep["grand_total"]
        self.assertEqual(sum(c["tokens"] for c in self.rep["cards"]), total)
        self.assertEqual(sum(self.rep["roles"].values()), total)
        self.assertEqual(sum(self.rep["outcomes"].values()), total)

    def test_flags_failed_runs_and_cards_over_twice_the_median(self):
        flags = {c["card"]: c["flags"] for c in self.rep["cards"]}
        self.assertEqual(self.rep["median"], 27.5)      # median of 150, 20, 25, 30
        self.assertEqual(flags["t_aaaaaaaa"], ["over_2x_median"])
        self.assertEqual(flags["t_cccccccc"], ["failed_run"])
        self.assertEqual(flags["t_bbbbbbbb"], [])      # a redo is a reason class, not a flag
        self.assertEqual(flags["t_dddddddd"], [])

    def test_top3_names_spenders_with_reason_class(self):
        top = [(c["card"], c["reason"]) for c in self.rep["top3"]]
        self.assertEqual(top, [("t_aaaaaaaa", "over_2x_median"), ("t_dddddddd", "ordinary"),
                               ("t_cccccccc", "failed_run")])
        t = Tree(tempfile.mkdtemp(dir=self.tmp.name))
        t.agent("r1", description="reviewer t_22222222", prompt="x", tokens=usage(9))
        t.agent("r2", description="reviewer t_22222222", prompt="x", tokens=usage(9))
        rep = token_accountant.report(token_accountant.scan(t.finish(), DAY))
        self.assertEqual([(c["card"], c["reason"]) for c in rep["top3"]], [("t_22222222", "redo")])

    def test_unattributed_is_its_own_row_and_never_a_card_flag_source(self):
        t = Tree(tempfile.mkdtemp(dir=self.tmp.name))
        t.agent("u", description="misc", prompt="none", tokens=usage(500))
        t.agent("k", description="implementer t_11111111", prompt="x", tokens=usage(10), notified=1)
        rep = token_accountant.report(token_accountant.scan(t.finish(), DAY))
        self.assertEqual([c["card"] for c in rep["cards"]], ["unattributed", "t_11111111"])
        self.assertEqual(rep["median"], 10)       # attributed cards only
        self.assertEqual(rep["grand_total"], 510)

    def test_json_data_carries_notified_and_cache_read_apart_from_spend(self):
        self.assertEqual((self.rep["notified"], self.rep["cache_read"]), (1330, 7))
        card = {c["card"]: c for c in self.rep["cards"]}["t_aaaaaaaa"]
        self.assertEqual((card["tokens"], card["notified"], card["cache_read"]), (150, 1300, 7))

    def test_render_prints_every_section_and_the_total(self):
        text = token_accountant.render(self.rep, DAY)
        for needle in ("tokens for 2026-10-01", "per card", "per role", "per outcome", "top 3",
                       "t_aaaaaaaa", "over_2x_median", "total 225", "fresh input + output + cache-creation"):
            self.assertIn(needle, text)

    def test_empty_day_reports_zero(self):
        rep = token_accountant.report([])
        self.assertEqual((rep["grand_total"], rep["cards"], rep["top3"]), (0, [], []))
        self.assertIn("no subagent runs", token_accountant.render(rep, DAY))


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = sample(self.tmp.name)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["tokens", *argv])
        return code, out.getvalue(), err.getvalue()

    def snapshot(self):
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(self.root).rglob("*")) if p.is_file()}

    def test_prints_table_for_the_day_and_never_writes(self):
        before = self.snapshot()
        code, out, _ = self.run_cli("--root", str(self.root), "--day", DAY)
        self.assertEqual(code, 0)
        self.assertIn("total 225", out)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(sorted(p.name for p in Path(self.tmp.name).iterdir()), ["-proj"])

    def test_a_home_or_ancestor_root_is_refused_with_exit_2(self):
        home = Path(tempfile.mkdtemp(dir=self.tmp.name)) / "home"
        (home / ".claude" / "projects").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            for bad in (home, home.parent, Path(home.anchor)):
                code, out, err = self.run_cli("--root", str(bad), "--day", DAY)
                self.assertEqual((code, out), (2, ""), bad)
                self.assertIn("home", err)
            code, _, _ = self.run_cli("--root", str(home / ".claude" / "projects"), "--day", DAY)
            self.assertEqual(code, 0)

    def test_json_flag(self):
        code, out, _ = self.run_cli("--root", str(self.root), "--day", DAY, "--json")
        self.assertEqual((code, json.loads(out)["grand_total"]), (0, 225))

    def test_missing_root_and_bad_day_are_clean_refusals(self):
        code, _, err = self.run_cli("--root", str(Path(self.tmp.name) / "nope"), "--day", DAY)
        self.assertEqual(code, 1)
        self.assertIn("no such directory", err)
        code, _, err = self.run_cli("--root", str(self.root), "--day", "yesterday")
        self.assertEqual(code, 1)
        self.assertIn("YYYY-MM-DD", err)


class Intent(unittest.TestCase):
    def test_ships_standalone_intent_examples(self):
        self.assertIn("token_accountant", intent.STANDALONE)
        self.assertTrue(1 <= len(intent.load("token_accountant")) <= 3)
        self.assertEqual(intent.lint(paths.INTENT / "token_accountant.jsonl", None, "token_accountant"), [])

    def test_every_example_is_judged_by_the_real_report(self):
        for ex in intent.load("token_accountant"):
            with self.subTest(ex["id"]):
                rep = token_accountant.report(ex["given"]["document"]["runs"])
                got = {"top3": [c["card"] for c in rep["top3"]], "reasons": [c["reason"] for c in rep["top3"]],
                       "grand_total": rep["grand_total"]}
                self.assertEqual(intent._differs(ex["expect"], got, None), [], ex["id"])


if __name__ == "__main__":
    unittest.main()
