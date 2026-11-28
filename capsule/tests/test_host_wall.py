"""The host wall (D161, D162): `capsule/runners/host_wall.py` planned, rendered and applied. Two
oracles: the planned/rendered text, and the real `sandbox-exec` (`test_host_wall_live`). The real
`claude` binary is `test_host_wall_cli`; the start-up check, `test_host_wall_apply`; the tick
closing a refused run, `test_host_wall_close` and `test_host_wall_outage`."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import incident_inputs, workspace
from capsule.runners import host, host_wall

DARWIN = unittest.skipUnless(sys.platform == "darwin", "sandbox-exec exists only on macOS")
NO_WRAP = "#!/bin/sh\nshift 2\nexec \"$@\"\n"     # a `sandbox-exec` that applies nothing


class Board(unittest.TestCase):
    """A board dir with a blind repo, spools `run0` (another run's) and `run1` (this one's), a
    claude config dir and a workspace."""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(os.path.realpath(td.name))
        self.board = self.root / "home" / "boards" / "b"
        self.spools = self.board / "spool"
        self.spool, self.other = self.spools / "run1", self.spools / "run0"
        self.repo, self.cfg, self.ws = self.root / "blind", self.root / "cfg", self.root / "ws"
        for d in (self.spool, self.other, self.repo, self.board / "blind-work" / "r9",
                  self.cfg / "projects", self.cfg / "jobs", self.ws):
            d.mkdir(parents=True)
        for f in (self.repo / "t.py", self.other / "report.json",
                  self.board / "blind-work" / "r9" / "t.py"):
            f.write_text("SECRET")
        (self.spool / "own.txt").write_text("OWN")
        patch = mock.patch.dict(os.environ, CLAUDE_CONFIG_DIR=str(self.cfg))
        patch.start()
        self.addCleanup(patch.stop)
        self.mode("report")

    def mode(self, mode, repo=None, **blind):
        cfg = {"board": "b", "redesign": {"blind_tests": mode},
               "blind_tests": dict({"repo": str(repo or self.repo)}, **blind)}
        (self.board / "config.json").write_text(json.dumps(cfg))

    def job(self, **over):
        return dict({"workspace": str(self.ws), "limits": {}, "inputs": {}}, **over)

    def plan(self, role="implementer", spool=None, **over):
        return host_wall.plan(role, spool or self.spool, self.job(**over))

    def run_job(self, spool, workspace):
        (spool / "job.json").write_text(json.dumps({"workspace": str(workspace)}))


class Plan(Board):
    def test_off_and_the_two_unwalled_roles_give_no_wall_and_every_other_role_is_walled(self):
        for role in ("validator", "blind-tester"):
            self.assertIsNone(self.plan(role))
        for role in ("implementer", "reviewer", "guide", "korin", "lander", "advisor", "new-role"):
            self.assertNotIn("unavailable", self.plan(role), role)
        self.mode("off")
        for role in ("implementer", "korin", "lander"):
            self.assertIsNone(self.plan(role), role)

    def test_a_plan_that_cannot_decide_never_fails_open(self):
        def gate(**blind):
            return json.dumps({"board": "b", "redesign": {"blind_tests": "gate"},
                               "blind_tests": dict({"repo": str(self.repo)}, **blind)})
        cases = [
            "{}", "[]", "null", "{not json", '"gate"',
            json.dumps({"board": "other", "redesign": {"blind_tests": "off"}}),
            json.dumps({"board": "b", "redesign": None}),
            json.dumps({"board": "b", "redesign": {"blind_tests": "banana"}}),
            json.dumps({"board": "b", "redesign": {"blind_tests": "gate"}}),
            json.dumps({"board": "b", "redesign": {"blind_tests": "gate"}, "blind_tests": {}}),
            gate(repo=5), gate(repo="a\0b"), gate(also_walled="x"), gate(also_walled=[1]),
        ]
        for text in cases:
            (self.board / "config.json").write_text(text)
            wall = self.plan()
            self.assertIn("unavailable", wall, text)
            with self.assertRaises(host_wall.WallUnavailable):
                host_wall.apply(["claude"], wall)
        self.mode("gate")
        for over in ({"workspace": None}, {"workspace": ""}, {"workspace": "/" + "a" * 250}):
            self.assertIn("unavailable", self.plan(**over), over)
        for odd in (self.root / "spool" / "x", Path("relative/x")):
            self.assertIn("unavailable", host_wall.plan("implementer", odd, self.job()))
        (self.board / "config.json").unlink()
        self.assertIn("no board config", self.plan()["unavailable"])

    def test_the_legacy_config_counts_only_for_its_own_board(self):
        (self.board / "config.json").unlink()
        legacy = self.root / "home" / "config.json"
        legacy.write_text(json.dumps({"board": "b", "redesign": {"blind_tests": "off"}}))
        self.assertIsNone(self.plan())
        legacy.write_text(json.dumps({"board": "z", "redesign": {"blind_tests": "off"}}))
        self.assertIn("unavailable", self.plan())

    def test_walled_folders_are_parents_and_real_paths(self):
        link = self.root / "link"
        link.symlink_to(self.repo)
        extra = self.root / "tests repo"
        extra.mkdir()
        self.mode("gate", repo=link, also_walled=[str(extra)])
        project = self.cfg / "projects" / host_wall._project(self.cfg, str(self.ws)).name
        for role in ("implementer", "reviewer", "guide"):
            wall = self.plan(role)
            self.assertEqual(wall["deny"], [str(p) for p in (
                self.repo, self.board / "blind-work", self.spools, self.cfg / "projects",
                self.cfg / "jobs", extra)])
            self.assertEqual(wall["meta"], [str(self.spools), str(self.cfg / "projects")])
            self.assertEqual(wall["allow"], [str(self.spool), str(project)])
            self.assertEqual((wall["blind"], wall["spool"]), ([], str(self.spool)))

    @DARWIN
    def test_a_data_volume_spelling_is_walled_at_the_plain_path(self):
        data = host_wall.DATA_VOLUME + str(self.repo)
        self.assertEqual(host_wall._canon(data), str(self.repo))
        self.mode("gate", repo=data, also_walled=[data])
        self.assertEqual(self.plan()["deny"][0], str(self.repo))

    def test_the_project_folder_is_the_clis_own_name_or_refused(self):
        name = host_wall._project(self.cfg, str(self.ws)).name
        self.assertEqual(name, "".join(c if c.isalnum() else "-" for c in str(self.ws)))
        host_wall._project(self.cfg, "/" + "a" * 199)
        with self.assertRaises(host_wall.WallUnavailable):
            host_wall._project(self.cfg, "/" + "a" * 200)

    def test_korin_gets_its_evidence_spools_minus_a_testers_and_never_a_blind_part(self):
        ev, tester, ghost, other = (self.spools / n for n in ("ev", "tester", "ghost", "oth"))
        for sp in (ev, tester, ghost, other):
            sp.mkdir()
        self.run_job(ev, self.ws)
        self.run_job(other, self.ws)
        self.run_job(tester, self.board / "blind-work" / "t1")      # ghost has no job.json at all
        foreign = self.root / "home" / "boards" / "z" / "spool" / "r"
        evidence = [str(ev / "worker.log"), str(tester), str(ghost), "event:3", 7, str(foreign),
                    str(other), str(ev)]
        wall = self.plan("korin", inputs={"evidence": evidence})
        self.assertEqual(wall["allow"][:3], [str(self.spool), str(ev), str(other)])
        self.assertEqual(len(wall["allow"]), 3)         # no project folder: no transcripts (S28)
        self.assertFalse([a for a in wall["allow"] if str(self.cfg / "projects") in a])
        self.assertNotIn("unavailable", self.plan("korin", workspace="/" + "a" * 250))
        self.assertEqual(wall["blind"], [str(self.spool), str(ev), str(other)])    # own spool too
        self.assertEqual(self.plan("korin")["allow"][0], str(self.spool))
        text = host_wall.render(wall)
        want = f'(deny file-read* (regex #"^{host_wall._esc(str(ev))}/([^/]*/)*blind"))'
        self.assertIn(want, text)
        self.assertEqual(self.plan("implementer", inputs={"evidence": evidence})["blind"], [])

    def test_a_validators_spool_with_blind_rows_is_not_given_to_korin_by_any_file(self):
        rows = {"outputs": [{"cmd": "blind:t1.test_x", "given": "G", "observed": "O"}]}
        deep = {"a": [{"b": {"cmd": "blind:t2.test_y"}}]}
        spools = {n: self.spools / n for n in ("rows", "deep", "plain", "badjson")}
        for sp in spools.values():
            sp.mkdir()
        for name, inputs in (("rows", rows), ("deep", deep),
                             ("plain", {"outputs": [{"cmd": "make test"}]})):
            (spools[name] / "job.json").write_text(json.dumps(
                {"workspace": str(self.ws), "inputs": inputs}))
        (spools["badjson"] / "job.json").write_text("{not json")
        wall = self.plan("korin", inputs={"evidence": [str(p) for p in spools.values()]})
        self.assertEqual(wall["allow"], [str(self.spool), str(spools["plain"])])
        for name in ("rows", "deep", "badjson"):            # a job that cannot be read counts too
            self.assertTrue(host_wall.is_tester_spool(spools[name]), name)
            for file in ("job.json", "worker.log", "report.json"):
                self.assertTrue(host_wall.withheld(spools[name] / file), (name, file))
        self.assertFalse(host_wall.is_tester_spool(spools["plain"]))
        self.assertFalse(host_wall.withheld(spools["plain"] / "job.json"))
        self.assertEqual(self.plan("implementer", inputs={"evidence": [str(spools["rows"])]})
                         ["allow"][0], str(self.spool))

    def test_the_profile_orders_denies_then_allows_then_the_blind_and_probe_denies(self):
        self.run_job(self.other, self.ws)
        wall = self.plan("korin", inputs={"evidence": [str(self.other)]})
        body = [ln for ln in host_wall.render(wall).splitlines() if not ln.startswith(";;")]
        at = {key: [i for i, ln in enumerate(body) if ln.startswith(key)] for key in (
            "(deny file-read* (subpath", "(allow file-read-metadata", "(allow file-read* (subpath",
            "(deny file-read* (regex")}
        self.assertEqual(body[:2], ["(version 1)", "(allow default)"])
        self.assertEqual([len(v) for v in at.values()], [1, 2, 1, 3])
        order = [v[0] for v in at.values()]
        self.assertEqual(order[0] < min(at["(allow file-read-metadata"]) < order[2] < order[3],
                         True, body)
        self.assertEqual(at["(deny file-read* (regex"][-1], len(body) - 1)
        self.assertIn(host_wall._esc(wall["probe"]), body[-1])      # the name, any tag after it

    def test_the_light_tier_is_refused_with_its_own_class_and_off_is_not(self):
        light = self.plan(limits={"tier": "light"})
        self.assertIn("cannot nest", light["unavailable"])
        with self.assertRaises(host_wall.LightTierRefused) as caught:
            host_wall.apply(["claude"], light)
        self.assertEqual((caught.exception.error_class, caught.exception.retry),
                         ("wall_light_tier", False))
        plain = host_wall.WallUnavailable("x")
        self.assertEqual((plain.error_class, plain.retry), ("wall_unavailable", True))
        self.mode("off")
        self.assertIsNone(self.plan(limits={"tier": "light"}))

    def test_a_refusal_for_one_job_has_its_own_class_and_config_trouble_does_not(self):
        self.assertEqual(host_wall.HOLD_CLASSES, ("wall_light_tier", "wall_job_refused"))
        for over in ({"workspace": None}, {"workspace": "/" + "a" * 250}):
            wall = self.plan(**over)
            self.assertEqual(wall["error_class"], "wall_job_refused")
            with self.assertRaises(host_wall.JobRefused) as caught:
                host_wall.apply(["claude"], wall)
            self.assertEqual((caught.exception.error_class, caught.exception.retry),
                             ("wall_job_refused", False))
        (self.board / "config.json").write_text("{}")
        self.assertEqual(self.plan()["error_class"], "wall_unavailable")
        self.mode("off")
        self.assertIsNone(self.plan(workspace="/" + "a" * 250))

    def test_light_runner_limits_carry_the_tier(self):
        runner = host.LightRunner()
        runner.enabled = True
        wt = self.root / "wt"
        wt.mkdir()
        name = runner.start("r" * 16, str(wt))
        self.assertEqual(runner.limits(name, str(self.spool))["tier"], "light")

    def test_hostile_paths_are_escaped_and_an_unquotable_one_refuses(self):
        hostile = 'a"b (c)\\d" ) (allow file-read*'
        wall = {"deny": [hostile], "meta": [], "allow": ["/x"], "blind": [], "probe": "/p"}
        text = host_wall.render(wall)
        self.assertIn('(subpath "a\\"b (c)\\\\d\\" ) (allow file-read*")', text)
        self.assertEqual(text.count("(allow file-read*"), 2)     # the hostile one is in a string
        for bad in ("a\nb", "a\0b"):
            with self.assertRaises(ValueError):
                host_wall.render(dict(wall, deny=[bad]))
        with self.assertRaises(ValueError):
            host_wall.render(dict(wall, blind=['/a"b']))
        self.assertEqual(host_wall._esc("/a.b(c)[d]"), "/a\\.b\\(c\\)\\[d\\]")

    def test_withheld_paths_are_a_testers_spool_and_any_blind_part_of_a_spool(self):
        tester = self.spools / "tester"
        tester.mkdir()
        self.run_job(tester, self.board / "blind-work" / "t1")
        self.run_job(self.spool, self.ws)
        (self.spool / "blind-out").mkdir()
        sp = self.spool
        for path, want in ((tester, True), (tester / "cli_result.txt", True),
                           (self.spools / "none" / "x", True), (sp / "blind-out" / "o", True),
                           (sp / "blind_result.json", True), (sp / "a" / "blindx", True),
                           (sp / "own.txt", False), (sp / "notblind", False),
                           (self.spools, False), (self.root / "elsewhere.log", False)):
            self.assertEqual(host_wall.withheld(path), want, str(path))
        text = "fatal: exit 1: THE TESTER'S LINE"
        evidence = ["event:1", str(tester), "free text"]
        for role in ("blind-tester", "blind_red:t_1", "blind_runner:t_1"):
            self.assertEqual(incident_inputs.blind_safe(role, text, evidence),
                             ("fatal", ["event:1", str(tester)]), role)
        for role in ("implementer", None):
            self.assertEqual(incident_inputs.blind_safe(role, text, evidence), (text, evidence))
        self.assertTrue(incident_inputs.blind_incident(["event:1"], "blind-tester"))
        self.assertFalse(incident_inputs.blind_incident(["event:1"], "implementer"))
        self.assertTrue(incident_inputs.blind_incident(["event:1", str(tester)]))
        self.assertFalse(incident_inputs.blind_incident(["event:1", str(self.spool), 5]))
        self.assertEqual(incident_inputs.scrub([str(tester)]), {"root_key": mock.ANY})
        self.assertEqual(incident_inputs.scrub([str(self.spool)]), {})
        why = f"host wall unavailable: the profile at {self.spools}/run1/hostwall.sb was refused"
        self.assertIsNone(host_wall.spool_of(why))              # a message is not a path (S30)
        self.assertEqual((host_wall.withheld(why), incident_inputs.scrub([why, "event:2"])),
                         (False, {}))
        (tester / "worker.log").write_text("BLIND TEXT")
        (self.spool / "worker.log").write_text("fine")
        self.assertIsNone(incident_inputs.first_tail((tester / "worker.log", tester), 100))
        self.assertEqual(incident_inputs.first_tail((sp / "worker.log",), 100)["tail"], "fine")


class Protected(unittest.TestCase):
    def test_the_wall_and_its_call_sites_are_protected(self):
        files = ["capsule/runners/host_wall.py", "capsule/worker.py", "capsule/models/claude.py",
                 "capsule/runners/host_wall/__init__.py", "capsule/models/claude/x.py",
                 "capsule/models/__init__.py"]
        self.assertEqual(workspace.touches_protected(files), files)


if __name__ == "__main__":
    unittest.main()


class BlindRowMatcher(Board):
    def test_a_row_spelled_with_case_spaces_or_backticks_is_withheld_like_validator_report(self):
        # followups line 349: one matcher (validator_report.is_blind) for both places
        for n, cmd in enumerate(("Blind:t1.test_x", "  blind:t1.test_x", "`blind:t1.test_x`")):
            sp = self.spools / f"odd{n}"
            sp.mkdir()
            (sp / "job.json").write_text(json.dumps(
                {"workspace": str(self.ws), "inputs": {"outputs": [{"cmd": cmd}]}}))
            self.assertTrue(host_wall.is_tester_spool(sp), cmd)
            self.assertTrue(host_wall.withheld(sp / "worker.log"), cmd)


class WalkCutLine(Board):
    def test_the_cut_line_says_it_counts_only_the_entries_it_met(self):
        # followups line 336: subtrees of cut entries are not counted, so the numbers are a floor
        import contextlib
        import io
        for n in range(4):
            (self.repo / "many" / f"d{n}").mkdir(parents=True)
        err = io.StringIO()
        with mock.patch.object(host_wall, "MAX_PROBED", 2), contextlib.redirect_stderr(err):
            host_wall._walk(str(self.repo / "many"))
        line = err.getvalue().strip()
        self.assertTrue(line.startswith("host wall: not probed under"), line)
        self.assertIn("2 past 2 in a folder", line)
        self.assertIn("at least", line)
        self.assertIn("not counted", line)


class AlsoWalledNamesAllowed(Board):
    def test_also_walled_over_an_allowed_path_is_refused_by_name_at_plan_time(self):
        # followups line 347: loud already (the proof fails); now the reason names the config key
        for walled in (self.spool, self.other.parent / "run1"):
            self.mode("gate", also_walled=[str(walled)])
            wall = self.plan()
            self.assertIn("also_walled", wall.get("unavailable", ""), walled)
            self.assertIn(str(walled), wall["unavailable"])
        for fine in (self.ws / "extra", self.spools):     # the spool root is walled already
            self.mode("gate", also_walled=[str(fine)])
            self.assertNotIn("unavailable", self.plan(), fine)
