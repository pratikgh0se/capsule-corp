"""EVAL-04 / COST-07: `golden swap`, `golden routing-diff`, `golden haiku-lint` against a fake
adapter (never a real model). The shipped `fixtures/golden/validator` has two cases, each
expecting {"verdict": "pass"}."""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import golden_cli, intent
from capsule.__main__ import main
from capsule.models import Result
from capsule.models.stub import StubAdapter

PASS, FAIL = {"verdict": "pass"}, {"verdict": "fail"}


def _entry(model, provider="claude"):
    return {"provider": provider, "account": "a", "model": model, "turn_budget": 3}


def _config(validator="m-base", **others):
    roles = {"implementer": "m", "reviewer": "m", "guide": "m", "validator": validator, **others}
    return {
        "board": "b", "epics": ["epic/x"], "contract_commands": ["true"],
        "caps": {"max_running": 1, "claim_ttl": 60, "failure_limit": 3, "checkpoint_chain": 1,
                 "tick_seconds": 30},
        "routing": {role: _entry(model) for role, model in roles.items()},
    }


class SwapCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        (self.home / "config.json").write_text(json.dumps(_config()))
        self.by_model = {}      # model -> list of canned replies, consumed across runs
        self.built, self.stubs = [], {}

    def adapter(self, route):
        self.built.append(route)
        if route["model"] not in self.stubs:        # one stub per model, replies span the runs
            self.stubs[route["model"]] = StubAdapter(self.by_model.get(route["model"], [PASS] * 20))
        return self.stubs[route["model"]]

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(golden_cli, "adapter_for", self.adapter):
            rc = main(["golden", *argv])
        return rc, out.getvalue(), err.getvalue()

    def swap(self, *extra, role="validator", to="stub/cand"):
        return self.cli("swap", role, "--to", to, "--capsule-home", str(self.home), *extra)


class Swap(SwapCase):
    def test_equal_candidate_passes_and_prints_table(self):
        rc, out, _ = self.swap()
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), [
            "01  base=1.00 cand=1.00", "02  base=1.00 cand=1.00",
            "swap validator claude/m-base -> stub/cand: PASS base=100% cand=100%"])

    def test_worse_candidate_blocks(self):
        self.by_model["cand"] = [PASS, FAIL]
        rc, out, _ = self.swap()
        self.assertEqual(rc, 1)
        self.assertIn("02  base=1.00 cand=0.00", out)
        self.assertIn(": BLOCK base=100% cand=50%", out)

    def test_better_candidate_passes(self):
        self.by_model["m-base"] = [FAIL, FAIL]
        rc, out, _ = self.swap()
        self.assertEqual(rc, 0)
        self.assertIn(": PASS base=0% cand=100%", out)

    def test_runs_replays_each_model_n_times(self):
        self.by_model["cand"] = [PASS, PASS, PASS, FAIL]       # run 1 all pass, run 2 case 02 fails
        rc, out, _ = self.swap("--runs", "2")
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.built), 2)
        self.assertIn("02  base=1.00 cand=0.50", out)
        self.assertIn("base=100% cand=75%", out)

    def test_fallback_is_disabled_on_both_routes(self):
        cfg = _config()
        cfg["routing"]["validator"]["fallback"] = "claude-haiku-4-5"
        (self.home / "config.json").write_text(json.dumps(cfg))
        self.swap()
        self.assertTrue(self.built)
        self.assertTrue(all(r["fallback"] is None for r in self.built))
        self.assertEqual({r["model"] for r in self.built}, {"m-base", "cand"})

    def test_candidate_call_failure_blocks_even_when_graded_equal(self):
        fatal = Result(None, 1, {}, 1, "fatal", None, None, "boom", "")
        self.by_model["cand"] = [fatal, PASS]
        rc, out, err = self.swap()
        self.assertEqual(rc, 1)
        self.assertIn("BLOCK", out)
        self.assertIn("model call failed", err)

    def test_no_cases_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(golden_cli, "FIXTURES", Path(tmp)):
            rc, out, err = self.swap()
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("no golden cases", err)
        self.assertEqual(self.built, [])

    def test_bad_to_or_unreadable_config_exits_3(self):
        self.assertEqual(self.swap(to="just-a-model")[0], 3)
        self.assertEqual(self.swap(to="stub/")[0], 3)
        (self.home / "config.json").write_text("{not json")
        self.assertEqual(self.swap()[0], 3)
        self.assertEqual(self.built, [])

    def test_role_with_cases_but_no_route_exits_3(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = Path(tmp) / "nobody" / "01"
            case.mkdir(parents=True)
            (case / "case.json").write_text(json.dumps({"bundle": {"prompt": "p"}, "expect": PASS}))
            with mock.patch.object(golden_cli, "FIXTURES", Path(tmp)):
                rc, out, err = self.swap(role="nobody")
        self.assertEqual((rc, out), (3, ""))
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.built, [])

    def test_role_without_cases_exits_2(self):
        self.assertEqual(self.swap(role="nobody")[0], 2)

    def test_unknown_provider_exits_3_without_traceback(self):
        """The real `adapter_for` (no fake): an unknown provider in --to or in the route."""
        for route_provider, to in (("claude", "nosuch/model"), ("nosuch", "stub/cand")):
            cfg = _config()
            cfg["routing"]["validator"]["provider"] = route_provider
            (self.home / "config.json").write_text(json.dumps(cfg))
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main(["golden", "swap", "validator", "--to", to,
                           "--capsule-home", str(self.home)])
            self.assertEqual((rc, out.getvalue()), (3, ""), to)
            self.assertEqual(len(err.getvalue().strip().splitlines()), 1)
            self.assertIn("unknown adapter", err.getvalue())

    def test_baseline_outage_blocks_even_when_candidate_is_fine(self):
        fatal = Result(None, 1, {}, 1, "fatal", None, None, "boom", "")
        self.by_model["m-base"] = [fatal, fatal]
        rc, out, err = self.swap()
        self.assertEqual(rc, 1)
        self.assertIn("BLOCK", out)
        self.assertIn("baseline model call failed", err)

    def test_a_call_that_raises_is_a_failed_call(self):
        class Boom:
            def run(self, *args):
                raise RuntimeError("down")
        self.stubs["cand"] = Boom()
        rc, out, err = self.swap()
        self.assertEqual(rc, 1)
        self.assertIn("BLOCK", out)
        self.assertIn("RuntimeError", err)


class IntentReplay(SwapCase):
    """Replays of the swap rows by id (contracts/intent/golden_cli.jsonl, written by the
    coordinator): the scenario is fixed here, the row's `expect.exit` is checked when present."""

    def expected(self, row_id, default):
        rows = [e for e in intent.load("golden_cli") if e["id"] == row_id]
        return rows[0]["expect"]["exit"] if rows else default

    def test_swap_baseline_outage_blocks(self):
        fatal = Result(None, 1, {}, 1, "fatal", None, None, "boom", "")
        self.by_model["m-base"] = [fatal, fatal]
        rc, out, _ = self.swap()
        self.assertEqual(rc, self.expected("swap-baseline-outage-blocks", 1))
        self.assertIn("BLOCK", out)

    def test_swap_lower_candidate_blocks(self):
        self.by_model["cand"] = [PASS, FAIL]
        rc, out, _ = self.swap()
        self.assertEqual(rc, self.expected("swap-lower-candidate-blocks", 1))
        self.assertIn("BLOCK base=100% cand=50%", out)

    def test_golden_run_still_works(self):
        rc, out, _ = self.cli("run", "validator", "--capsule-home", str(self.home))
        self.assertEqual(rc, 0)
        self.assertIn("passed 2/2 (100%), min 100%", out)


class RoutingDiff(SwapCase):
    def write(self, name, cfg):
        path = self.home / name
        path.write_text(json.dumps(cfg) if not isinstance(cfg, str) else cfg)
        return str(path)

    def test_same_routing_exits_0_and_prints_nothing(self):
        a, b = self.write("a.json", _config()), self.write("b.json", _config())
        self.assertEqual(self.cli("routing-diff", a, b), (0, "", ""))

    def test_changed_added_removed_roles(self):
        new = _config(validator="m-new")
        new["routing"]["reviewer"]["provider"] = "other"
        del new["routing"]["guide"]
        new["routing"]["scribe"] = _entry("m")
        rc, out, _ = self.cli("routing-diff", self.write("a.json", _config()),
                              self.write("b.json", new))
        self.assertEqual(rc, 1)
        self.assertEqual(sorted(out.splitlines()), sorted([
            "validator claude/m-base -> claude/m-new", "reviewer claude/m -> other/m",
            "+scribe", "-guide"]))

    def test_unreadable_input_exits_2(self):
        good = self.write("a.json", _config())
        for bad in (str(self.home / "missing.json"), self.write("bad.json", "{nope"),
                    self.write("noroute.json", {"x": 1})):
            self.assertEqual(self.cli("routing-diff", good, bad)[0], 2)
            self.assertEqual(self.cli("routing-diff", bad, good)[0], 2)


class HaikuLint(SwapCase):
    def lint(self, cfg):
        (self.home / "config.json").write_text(json.dumps(cfg))
        return self.cli("haiku-lint", "--capsule-home", str(self.home))

    def test_no_haiku_roles(self):
        self.assertEqual(self.lint(_config()), (0, "no haiku roles\n", ""))

    def test_clean_contract_is_ok(self):
        with mock.patch("capsule.contracts.render", return_value="Do the work. Rethinker no."):
            rc, out, _ = self.lint(_config(guide="claude-HAIKU-4-5"))
        self.assertEqual((rc, out), (0, "ok guide\n"))

    def test_banned_word_in_any_case_fails(self):
        for text, word in (("Think first", "Think"), ("he thinks", "thinks"),
                           ("THINKING", "THINKING")):
            with mock.patch("capsule.contracts.render", return_value=text):
                rc, out, _ = self.lint(_config(guide="claude-haiku-4-5"))
            self.assertEqual((rc, out), (1, f"banned guide: {word}\n"))

    def test_only_whole_words_are_banned(self):
        with mock.patch("capsule.contracts.render", return_value="rethinking a thinker; unthink"):
            rc, out, _ = self.lint(_config(guide="Claude-HaIkU-4-5"))
        self.assertEqual((rc, out), (0, "ok guide\n"))

    def test_only_haiku_roles_are_checked(self):
        seen = []

        def render(role):
            seen.append(role)
            return "fine"
        with mock.patch("capsule.contracts.render", render):
            self.lint(_config(guide="claude-haiku-4-5", reviewer="haiku-x"))
        self.assertEqual(sorted(seen), ["guide", "reviewer"])


if __name__ == "__main__":
    unittest.main()
