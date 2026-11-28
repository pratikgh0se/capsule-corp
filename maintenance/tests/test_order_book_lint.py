# mutation-check: targets-predate-range b957739c9 -- fixture rename only, no behaviour pinned ::
#   WeeklyJob.test_the_label_is_loaded_and_custodial
import contextlib, io, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("AGENT_OPS_NO_SEND", "1")
import order_book_lint as obl

TODAY = "2026-10-27"
INTENT = Path(__file__).resolve().parents[2] / "contracts" / "intent" / "order_book_lint.jsonl"
BET = ("{slug} · appetite 6 cards / 2 epic runs / 10% window · one number: one video "
       "published via the ledger · kill by {kill} <!-- dictated -->")
OPP = "- A creator publishes nothing because every step needs a person · witness: {w} · 2026-09-12"
WIT = "- {w} · 2026-09-12 · owner (dictated in chat) · [[Knowledge/Witness {w}]]"
PITCH = ("---\nslug: s\n---\n\n## Problem\n\nWitness {w} said a creator publishes nothing.\n\n"
         "## Appetite\n\n6 cards.\n")
LOG = ("| 1 | first | a decision | adopted |\n"
       "| 2 | Outcome: alpha (2026-10-20) | one video -> keep; it worked -> row 1 | outcome |\n")
from capsule.tests.public_tree import private_only  # noqa: E402


def book(live=("alpha 2099-01-01",), deck=(), opps=(OPP.format(w="w1"),), killed=(),
         wits=(WIT.format(w="w1"),), drop=None):
    def bets(items):
        return [f"- {BET.format(slug=s.split()[0], kill=s.split()[1])}" for s in items]
    sections = {"Live bet": bets(live), "On deck": bets(deck), "Opportunities": list(opps),
                "Killed": list(killed), "Witnesses": list(wits)}
    out = ["## Policy\n\n- **One live bet.**\n"]
    for name, lines in sections.items():
        if name != drop:
            comment = "<!-- template comment\n- not an entry -->"
            out.append(f"## {name}\n\n{comment}\n" + "\n".join(lines) + "\n")
    return "\n".join(out)


class Fixture(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.vault = Path(self._td.name)
        (self.vault / "Boards").mkdir()
        (self.vault / "Pitches").mkdir()

    def pitch(self, slug, w="w1"):
        (self.vault / "Pitches" / f"{slug}.md").write_text(PITCH.format(w=w), encoding="utf-8")

    def lint(self, text, rows=None, today=TODAY):
        return obl.lint(text, self.vault, rows, today)


class Clean(Fixture):
    def test_a_clean_book_reports_nothing(self):
        self.pitch("alpha")
        self.pitch("beta")
        text = book(deck=("beta 2099-02-01",),
                    killed=("- old · 2026-09-20 · no witness came back",))
        self.assertEqual(self.lint(text), [])

    def test_an_empty_book_with_every_section_is_clean(self):
        self.assertEqual(self.lint(book(live=(), opps=(), wits=())), [])

    def test_a_template_comment_is_not_an_entry(self):
        self.assertEqual(self.lint(book(live=(), opps=(), wits=())), [])
        self.assertEqual(obl._parse_sections(book())["Opportunities"][0][1][:3], "- A")

    def test_an_opportunity_may_cite_two_witnesses(self):
        text = book(live=(), opps=(OPP.format(w="w1, w2"),),
                    wits=(WIT.format(w="w1"), WIT.format(w="w2")))
        self.assertEqual(self.lint(text), [])


class WitnessRule(Fixture):
    def test_an_opportunity_without_a_witness_tail(self):
        out = self.lint(book(live=(), opps=("- A creator publishes nothing",)))
        self.assertEqual(len(out), 1)
        self.assertIn("no `witness: <id> · <YYYY-MM-DD>` tail", out[0])

    def test_an_opportunity_with_an_undated_witness(self):
        out = self.lint(book(live=(), opps=("- A creator fails · witness: w1",)))
        self.assertEqual(len(out), 1)

    def test_an_unindexed_witness_id(self):
        out = self.lint(book(live=(), opps=(OPP.format(w="w9"),)))
        self.assertEqual(len(out), 1)
        self.assertIn("witness 'w9' is not indexed", out[0])

    def test_an_index_line_missing_its_link(self):
        out = self.lint(book(live=(), wits=("- w1 · 2026-09-12 · owner",)))
        self.assertTrue(any("Witnesses: index line needs" in p for p in out), out)

    def test_an_index_line_with_a_bad_date(self):
        out = self.lint(book(live=(), wits=("- w1 · sometime · owner · [[x]]",)))
        self.assertTrue(any("index line needs" in p for p in out), out)

    def test_a_duplicate_witness_id(self):
        out = self.lint(book(live=(), wits=(WIT.format(w="w1"), WIT.format(w="w1"))))
        self.assertEqual(len(out), 1)
        self.assertIn("already indexed", out[0])

    def test_a_pitch_problem_that_cites_no_witness(self):
        self.pitch("alpha", w="nobody")
        out = self.lint(book())
        self.assertEqual(len(out), 1)
        self.assertIn("## Problem cites no witness id", out[0])

    def test_a_witness_id_inside_a_longer_word_is_not_a_citation(self):
        self.pitch("alpha", w="w10")
        self.assertEqual(len(self.lint(book())), 1)


class Caps(Fixture):
    def test_two_live_bets(self):
        out = self.lint(book(live=("alpha 2099-01-01", "beta 2099-01-01")))
        self.assertEqual(out, ["Live bet: 2 bets, over the cap of 1"])

    def test_two_on_deck_each_needing_a_pitch(self):
        out = self.lint(book(live=(), deck=("beta 2099-01-01", "gamma 2099-01-01")))
        self.assertEqual(out[0], "On deck: 2 bets, over the cap of 1")
        self.assertEqual(len([p for p in out if "no Pitches/" in p]), 2)

    def test_fifteen_opportunities_pass_and_sixteen_fail(self):
        opps = tuple(OPP.format(w="w1") for _ in range(15))
        self.assertEqual(self.lint(book(live=(), opps=opps)), [])
        out = self.lint(book(live=(), opps=opps + (OPP.format(w="w1"),)))
        self.assertEqual(out, ["Opportunities: 16 lines, over the cap of 15"])

    def test_a_non_bullet_line_cannot_dodge_the_cap(self):
        out = self.lint(book(live=(), opps=("A creator fails · witness: w1 · 2026-09-12",)))
        self.assertEqual(len(out), 1)
        self.assertIn("not a `- ` entry", out[0])

    def test_on_deck_without_its_pitch_file(self):
        out = self.lint(book(live=(), deck=("beta 2099-01-01",)))
        self.assertEqual(len(out), 1)
        self.assertIn("beta has no Pitches/beta.md", out[0])

    def test_a_malformed_bet_line(self):
        out = self.lint(book(live=()).replace("## On deck\n", "## On deck\n- just an idea\n"))
        self.assertTrue(any("On deck: not `<slug> · appetite" in p for p in out), out)

    def test_a_malformed_killed_line(self):
        out = self.lint(book(live=(), killed=("- dropped it",)))
        self.assertEqual(len(out), 1)
        self.assertIn("Killed: not `<slug>", out[0])

    def test_a_missing_section(self):
        out = self.lint(book(live=(), drop="Killed"))
        self.assertEqual(out, ["missing section `## Killed`"])


class KillRule(Fixture):
    def test_lapsed_kill_with_no_outcome_row(self):
        self.pitch("alpha")
        out = self.lint(book(live=("alpha 2026-10-10",)), rows=[])
        self.assertEqual(len(out), 1)
        self.assertIn("alpha kill date 2026-10-10 passed with no Outcome row", out[0])

    def test_lapsed_kill_with_an_outcome_row_is_clean(self):
        self.pitch("alpha")
        rows = obl.decision_log.parse(LOG)
        self.assertEqual(self.lint(book(live=("alpha 2026-10-10",)), rows=rows), [])

    def test_an_outcome_row_for_another_bet_does_not_count(self):
        self.pitch("alpha")
        rows = obl.decision_log.parse(LOG.replace("Outcome: alpha", "Outcome: alphabet"))
        self.assertEqual(len(self.lint(book(live=("alpha 2026-10-10",)), rows=rows)), 1)

    def test_a_kill_date_of_today_has_not_lapsed(self):
        self.pitch("alpha")
        self.assertEqual(self.lint(book(live=(f"alpha {TODAY}",)), rows=[]), [])

    def test_a_missing_decision_log_still_reports_the_lapse(self):
        self.pitch("alpha")
        out = self.lint(book(live=("alpha 2026-10-10",)), rows=None)
        self.assertIn("no decision log found", out[0])

    def test_status_outcome_row_with_the_slug_counts(self):
        self.pitch("alpha")
        rows = obl.decision_log.parse("| 7 | verdict on alpha (2026-10-12) | keep | outcome |\n")
        self.assertEqual(self.lint(book(live=("alpha 2026-10-10",)), rows=rows), [])


class Cli(Fixture):
    def run_main(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        old = os.environ.get("OBSIDIAN_VAULT_PATH")
        os.environ.pop("OBSIDIAN_VAULT_PATH", None)
        if env:
            os.environ["OBSIDIAN_VAULT_PATH"] = env
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = obl.main(list(argv))
        finally:
            os.environ.pop("OBSIDIAN_VAULT_PATH", None)
            if old is not None:
                os.environ["OBSIDIAN_VAULT_PATH"] = old
        return rc, out.getvalue(), err.getvalue()

    def write_book(self, text):
        p = self.vault / "Boards" / "Order Book.md"
        p.write_text(text, encoding="utf-8")
        return p

    def test_clean_book_exits_zero_via_the_vault_env(self):
        self.write_book(book(live=(), opps=(), wits=()))
        rc, out, _ = self.run_main("--today", TODAY, env=str(self.vault))
        self.assertEqual((rc, out), (0, "order-book lint: 0 problem(s)\n"))

    def test_violation_exits_one_with_plain_one_line_findings(self):
        self.write_book(book(live=(), opps=("- A creator fails",)))
        rc, out, _ = self.run_main("--today", TODAY, env=str(self.vault))
        self.assertEqual(rc, 1)
        lines = out.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].endswith("1 problem(s)"))

    def test_an_explicit_path_beats_the_env_and_finds_its_own_vault(self):
        p = self.write_book(book(live=(), opps=(), wits=()))
        other = tempfile.mkdtemp()
        self.addCleanup(lambda: os.rmdir(other))
        rc, _, _ = self.run_main(str(p), "--today", TODAY, env=other)
        self.assertEqual(rc, 0)

    def test_the_decision_log_is_found_under_the_vault(self):
        (self.vault / "Decisions").mkdir()
        name = "Capsule Corp - System design decisions 2026-09-06.md"
        (self.vault / "Decisions" / name).write_text(LOG, encoding="utf-8")
        self.pitch("alpha")
        p = self.write_book(book(live=("alpha 2026-10-10",)))
        self.assertEqual(self.run_main(str(p), "--today", TODAY)[0], 0)

    def test_no_book_and_no_env_is_a_usage_error(self):
        rc, _, err = self.run_main()
        self.assertEqual(rc, 2)
        self.assertIn("usage", err)

    def test_a_missing_book_exits_two(self):
        rc, _, err = self.run_main(str(self.vault / "nope.md"))
        self.assertEqual(rc, 2)
        self.assertIn("no order book at", err)

    def test_it_is_wired_to_no_gate(self):
        repo = Path(__file__).resolve().parents[2]
        for p in (repo / "maintenance" / "report.sh", repo / "deploy.sh",
                  repo / ".github" / "workflows" / "verify.yml"):
            if p.exists():
                self.assertNotIn("order_book_lint", p.read_text(encoding="utf-8"), str(p))


class WeeklyJob(unittest.TestCase):
    """M243: the weekly launchd job is loaded (owner said go; it is not in launchd/unloaded)."""
    REPO = Path(__file__).resolve().parents[2]
    REPO_TEMPLATES = REPO / "launchd" / "templates"
    # the overlay template carries its own label prefix in the file name
    LABEL = next((p.stem for p in sorted(REPO_TEMPLATES.glob("*.obsidian-order-book-lint.plist"))),
                 "obsidian-order-book-lint")

    def read(self, rel):
        return (self.REPO / rel).read_text(encoding="utf-8")

    @private_only
    def test_the_plist_runs_run_sh_weekly_monday_0900(self):
        plist = self.read(f"launchd/templates/{self.LABEL}.plist")
        self.assertIn(f"<key>Label</key><string>{self.LABEL}</string>", plist)
        self.assertIn("<string>order-book-lint</string>", plist)
        for key, n in (("Weekday", 1), ("Hour", 9), ("Minute", 0)):
            self.assertIn(f"<key>{key}</key><integer>{n}</integer>", plist)

    @private_only
    def test_the_label_is_loaded_and_custodial(self):
        self.assertNotIn(self.LABEL, self.read("launchd/unloaded").splitlines())
        self.assertIn("${_OP}.obsidian-order-book-lint", self.read("bin/lib_jobs.sh"))

    def test_status_cadence_lists_the_job(self):
        self.assertIn('"order-book-lint": 7*86400', self.read("maintenance/status.sh"))

    def test_run_sh_dry_run_names_the_lint_and_needs_no_prompt_file(self):
        with tempfile.TemporaryDirectory() as t:
            env = dict(os.environ, HOME=t, OBSIDIAN_VAULT_PATH=t, RUN_SH_DRY_RUN="1",
                       RUN_SH_MAINT_DIR=str(self.REPO / "maintenance"))
            r = subprocess.run(["bash", str(self.REPO / "maintenance" / "run.sh"),
                                "order-book-lint"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("dry run: would run order_book_lint.py", r.stdout)
        self.assertNotIn("no prompt file", r.stderr)
        self.assertNotIn("model", r.stdout)

    def run_job(self, book_text):
        """`run.sh order-book-lint` for real, in a scratch HOME / vault / MAINT_DIR (the
        repo's lint script symlinked in). (exit code, the job log's text)"""
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            maint, vault = root / "maint", root / "vault"
            maint.mkdir()
            (vault / "Boards").mkdir(parents=True)
            (maint / "order_book_lint.py").symlink_to(
                self.REPO / "maintenance" / "order_book_lint.py")
            if book_text is not None:
                (vault / "Boards" / "Order Book.md").write_text(book_text, encoding="utf-8")
            env = dict(os.environ, HOME=str(root), OBSIDIAN_VAULT_PATH=str(vault),
                       RUN_SH_MAINT_DIR=str(maint), AGENT_OPS_STATE_DIR=str(root / "state"),
                       AGENT_OPS_NO_SEND="1")
            r = subprocess.run(["bash", str(self.REPO / "maintenance" / "run.sh"),
                                "order-book-lint"], env=env, capture_output=True, text=True)
            logs = "".join(p.read_text(encoding="utf-8")
                           for p in (maint / "logs").glob("order-book-lint-*.log"))
        return r.returncode, logs

    def test_a_policy_violation_is_logged_and_the_job_still_exits_zero(self):
        rc, log = self.run_job(book(opps=[OPP.format(w="w9")]))   # w9 is not indexed
        self.assertEqual(rc, 0, log)
        self.assertIn("order-book lint: 1 problem(s)", log)

    def test_a_missing_book_fails_the_job(self):
        rc, log = self.run_job(None)
        self.assertNotEqual(rc, 0, log)
        self.assertIn("no order book at", log)


class IntentReplay(Fixture):
    """Every `contracts/intent/order_book_lint.jsonl` example, replayed through main()."""

    def books(self, given):
        """(book text, decision-log text) built from one example's `given`."""
        if "opportunity_witnesses" in given:
            ids = given["opportunity_witnesses"]
        else:
            ids = ["w1"] * given.get("opportunities", 1)
        wits = given.get("witnesses_indexed", ["w1"])
        live = (f"{given['live_bet']} {given['kill_by']}",) if "live_bet" in given else ()
        text = book(live=live, opps=[OPP.format(w=w) for w in ids],
                    wits=[WIT.format(w=w) for w in wits])
        slug = given.get("outcome_row_for")
        return text, (f"| 2 | Outcome: {slug} (2026-10-20) | one video -> keep | outcome |\n"
                      if slug else "| 1 | first | a decision | adopted |\n")

    def test_every_intent_example_replays(self):
        self.assertTrue(INTENT.is_file(), f"{INTENT} is missing")
        docs = [json.loads(ln) for ln in INTENT.read_text().splitlines() if ln.strip()]
        self.assertEqual(len(docs), 3)
        self.pitch("alpha")
        (self.vault / "Decisions").mkdir()
        for ex in docs:
            given, expect = ex["given"]["document"], ex["expect"]
            text, log = self.books(given)
            name = "Capsule Corp - System design decisions 2026-09-06.md"
            (self.vault / "Decisions" / name).write_text(log, encoding="utf-8")
            path = self.vault / "Boards" / "Order Book.md"
            path.write_text(text, encoding="utf-8")
            argv = [str(path)] + (["--today", given["today"]] if "today" in given else [])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = obl.main(argv)
            lines = out.getvalue().splitlines()
            self.assertEqual(rc, expect["exit"], ex["id"] + out.getvalue())
            self.assertEqual(len(lines) - 1, expect["problems"], ex["id"] + out.getvalue())
            if expect["problems"]:
                self.assertIn(expect["problem_names"], out.getvalue(), ex["id"])


class HelpText(unittest.TestCase):
    def test_help_names_the_weekly_job_not_no_job(self):
        out = subprocess.run([sys.executable, obl.__file__, "--help"], capture_output=True,
                             text=True, check=True).stdout
        flat = " ".join(out.split())
        self.assertIn("run weekly by the order-book-lint job", flat)
        self.assertNotIn("no job", flat)


if __name__ == "__main__":
    unittest.main()
