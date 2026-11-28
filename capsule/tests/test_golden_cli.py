# mutation-check: targets-predate-range 2548512946 -- a test-only adaptation: the read guard
#   added in this range refuses a relative workspace, so the argv check now passes an
#   absolute one; the no-fallback behaviour it tests predates the range ::
#   ModelUnavailable.test_unavailable_model_fails_loudly_and_never_falls_back
"""EVAL-04: `python3 -m capsule golden run <role>` against the stub adapter (never a real model).
Test names carry the intent row ids (contracts/intent/golden_cli.jsonl); the fixture set is the
shipped `fixtures/golden/validator`, whose two cases each expect {"verdict": "pass"}."""
import contextlib
import io
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import contracts, golden_cli, intent, paths
from capsule.__main__ import main
from capsule.jsonschema import load_all
from capsule.models import Result, claude
from capsule.models.stub import StubAdapter

sys.path.insert(0, str(paths.REPO / "maintenance"))
import public_lint  # noqa: E402

PASS = {"verdict": "pass"}
FIXTURE_DIR = paths.REPO / "capsule" / "tests" / "fixtures" / "golden"


def _route(model, **extra):
    return {"provider": "claude", "account": "a", "model": model, "turn_budget": 3, **extra}


def _config(validator_route):
    return {
        "board": "b", "epics": ["epic/x"], "contract_commands": ["true"],
        "caps": {"max_running": 1, "claim_ttl": 60, "failure_limit": 3, "checkpoint_chain": 1,
                 "tick_seconds": 30},
        "routing": {"implementer": _route("m"), "reviewer": _route("m"), "guide": _route("m"),
                    "validator": validator_route},
    }


class RecordingStub(StubAdapter):
    """The stub, plus what the CLI handed it."""

    def __init__(self, reports, route, seen):
        super().__init__(reports)
        seen.append({"route": route, "calls": []})
        self._seen = seen[-1]

    def run(self, role, bundle, workspace, limits):
        self._seen["calls"].append((role, bundle, Path(workspace).name, limits))
        return super().run(role, bundle, workspace, limits)


class GoldenCliCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.seen = []
        self.reports = []

    def write_config(self, validator_route):
        (self.home / "config.json").write_text(json.dumps(_config(validator_route)))

    def golden(self, *args, fixtures=None, role="validator"):
        """Run the verb; returns (exit code, stdout, stderr)."""
        def factory(route):
            return RecordingStub(self.reports, route, self.seen)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(golden_cli, "adapter_for", factory), \
                mock.patch.object(golden_cli, "FIXTURES", fixtures or FIXTURE_DIR):
            rc = main(["golden", "run", role, "--capsule-home", str(self.home), *args])
        return rc, out.getvalue(), err.getvalue()

    def rows(self, out):
        """{case: PASS|FAIL} from the printed table."""
        return {parts[0]: parts[1] for parts in (line.split() for line in out.splitlines())
                if len(parts) == 2 and parts[1] in ("PASS", "FAIL")}


class BelowThreshold(GoldenCliCase):
    def test_golden_below_threshold_exits_nonzero(self):
        self.write_config(_route("m1"))
        self.reports = [PASS, PASS]
        rc, out, _ = self.golden()
        self.assertEqual(rc, 0)
        self.assertEqual(self.rows(out), {"01": "PASS", "02": "PASS"})
        self.seen.clear()
        self.reports = [PASS, {"verdict": "fail"}]          # case 02 answered wrongly
        rc, out, _ = self.golden()
        self.assertEqual(rc, 1)
        self.assertEqual(self.rows(out), {"01": "PASS", "02": "FAIL"})
        self.seen.clear()
        self.reports = [PASS, {"verdict": "fail"}]          # 1 of 2 = 0.5 meets --min-pass 0.5
        self.assertEqual(self.golden("--min-pass", "0.5")[0], 0)

    def test_table_equals_hand_count(self):
        """Oracle two: read each fixture's `expect` and the replies by hand, compare the table."""
        self.write_config(_route("m1"))
        replies = [{"verdict": "pass", "note": "extra"}, {"verdict": "fail"}]
        expects = [json.loads((FIXTURE_DIR / "validator" / n / "case.json").read_text())["expect"]
                   for n in ("01", "02")]
        by_hand = ["PASS" if all(r.get(k) == v for k, v in e.items()) else "FAIL"
                   for e, r in zip(expects, replies)]
        self.assertEqual(by_hand, ["PASS", "FAIL"])
        self.reports = replies
        rc, out, _ = self.golden()
        self.assertEqual(list(self.rows(out).values()), by_hand)
        self.assertEqual(rc, 1)
        self.assertIn("1/2", out)


class EmptySet(GoldenCliCase):
    def test_empty_golden_set_is_error_not_pass(self):
        self.write_config(_route("m1"))
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "validator").mkdir()                       # present but holds no case
            (Path(tmp) / "validator" / "stray").mkdir()             # a dir without case.json
            for fixtures in (Path(tmp), Path(tmp) / "nowhere"):    # empty, then missing
                rc, out, err = self.golden("--min-pass", "0", fixtures=fixtures)
                self.assertEqual(rc, 2)
                self.assertNotIn("PASS", out)
                self.assertIn("no golden cases", err)
        self.assertEqual(self.seen, [])         # no model is ever built for a set that is not there


class GraderSemantics(GoldenCliCase):
    def test_grader_is_dict_subset_equality(self):
        self.write_config(_route("m1"))
        for replies, want in (
            ([{"verdict": "pass", "extra": 1}, PASS], {"01": "PASS", "02": "PASS"}),  # extra ok
            ([{"verdict": "fail"}, PASS], {"01": "FAIL", "02": "PASS"}),  # wrong value
            ([{"other": "pass"}, PASS], {"01": "FAIL", "02": "PASS"}),  # missing key
            ([None, PASS], {"01": "FAIL", "02": "PASS"}),  # no report
            (["pass", PASS], {"01": "FAIL", "02": "PASS"}),  # not a dict
        ):
            self.reports = replies
            self.assertEqual(self.rows(self.golden()[1]), want, replies)

    def test_replay_gives_the_adapter_a_real_run_shape(self):
        """The real adapter needs bundle["system"] and limits["schema"]; the shim supplies them."""
        self.write_config(_route("m1"))
        self.reports = [PASS, PASS]
        self.golden()
        role, bundle, case, limits = self.seen[0]["calls"][0]
        self.assertEqual((role, case), ("validator", "01"))
        self.assertEqual(bundle["prompt"], "case 01: check the doc")
        self.assertEqual(bundle["system"], contracts.render("validator"))
        self.assertEqual(limits["schema"], load_all(paths.SCHEMAS)["validator_result.v1"])


class SwapIsOneRoutingLine(GoldenCliCase):
    def test_swap_is_one_routing_line(self):
        self.reports = [PASS, PASS]
        self.write_config(_route("model-one"))
        self.golden()
        self.assertEqual(self.seen[0]["route"]["model"], "model-one")
        self.assertEqual(self.seen[0]["route"]["provider"], "claude")
        self.write_config(_route("model-two"))                  # the one line that changes
        self.reports = [PASS, PASS]
        self.golden()
        self.assertEqual(self.seen[1]["route"]["model"], "model-two")

    def test_route_flag_tries_a_candidate_without_editing_config(self):
        self.write_config(_route("model-one"))
        before = (self.home / "config.json").read_text()
        self.reports = [PASS, PASS]
        rc, out, _ = self.golden("--route", "stub/cand-x")
        self.assertEqual(rc, 0)
        route = self.seen[0]["route"]
        self.assertEqual((route["provider"], route["adapter"], route["model"]),
                         ("stub", "stub", "cand-x"))
        self.assertEqual(route["turn_budget"], 3)               # the rest of the routed entry stays
        self.assertIn("stub/cand-x", out)
        self.assertEqual((self.home / "config.json").read_text(), before)

    def test_route_flag_needs_provider_slash_model(self):
        self.write_config(_route("model-one"))
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
            self.golden("--route", "just-a-model")
        self.assertEqual(ctx.exception.code, 2)

    def test_unrouted_role_or_unreadable_config_is_an_error(self):
        for args in ([], ["--route", "stub/cand-x"]):           # no config under the home at all
            rc, _, err = self.golden(*args)
            self.assertEqual(rc, 3)
            self.assertIn("capsule golden", err)
        self.assertEqual(self.seen, [])


class ModelUnavailable(GoldenCliCase):
    """D44: a judgement role never falls back to Haiku; an unavailable model fails the run."""

    def test_unavailable_model_fails_loudly_and_never_falls_back(self):
        self.write_config(_route("model-one", fallback="claude-haiku-4-5"))
        fatal = Result(None, 1, {}, 1, "fatal", None, None, "boom", "")
        self.reports = [fatal, PASS]
        rc, out, err = self.golden("--min-pass", "0")           # a lenient threshold cannot hide it
        self.assertEqual(rc, 1)
        self.assertIn("model call failed", err)
        self.assertIn("fatal", err)
        self.assertIsNone(self.seen[0]["route"]["fallback"])    # the fallback never reaches it
        # second oracle: the argv the real adapter would build carries no --fallback-model,
        # while the same route with the config's fallback does (the control)
        _, bundle, _, limits = self.seen[0]["calls"][0]
        argv = claude.build_argv(self.seen[0]["route"], bundle, "/ws", limits)
        self.assertNotIn("--fallback-model", argv)
        with_fb = _route("model-one", fallback="claude-haiku-4-5")
        control = claude.build_argv(with_fb, bundle, "/ws", limits)
        self.assertIn("--fallback-model", control)


class FixtureHygiene(unittest.TestCase):
    def test_golden_fixture_is_scrubbed_and_public_lint_clean(self):
        files = sorted(str(p.relative_to(paths.REPO))
                       for p in FIXTURE_DIR.rglob("*") if p.is_file())
        self.assertTrue(files)
        # the full denylist when this checkout has one; the public edition has none
        has_list = (paths.REPO / public_lint.DEFAULT_DENYLIST).is_file()
        rules, exempt = public_lint.load_rules(paths.REPO, generic_only=not has_list)
        self.assertEqual(public_lint._scan(paths.REPO, files, rules, exempt), [])
        for name in files:
            if name.endswith("case.json"):
                case = json.loads((paths.REPO / name).read_text())
                self.assertTrue(case["expect"], name)       # an empty expect never passes


def _cases(root, role, n):
    """`n` golden cases under root/role, each expecting {"verdict": "pass"}."""
    for i in range(1, n + 1):
        case = Path(root) / role / f"{i:02d}"
        case.mkdir(parents=True)
        (case / "case.json").write_text(json.dumps({"bundle": {"prompt": "p"}, "expect": PASS}))


class IntentReplay(GoldenCliCase):
    """Each stamped row of contracts/intent/golden_cli.jsonl, replayed by id through the CLI."""

    def row(self, row_id):
        (example,) = [e for e in intent.load("golden_cli") if e["id"] == row_id]
        return example["given"]["document"], example["expect"]

    def test_golden_below_threshold_exits_nonzero(self):
        doc, expect = self.row("golden-below-threshold-exits-nonzero")
        self.write_config(_route("m1"))
        with tempfile.TemporaryDirectory() as tmp:
            _cases(tmp, doc["role"], doc["cases"])
            for name, run in doc["runs"].items():
                matching = doc["stub_replies_matching_expect"]
                self.reports = [PASS] * matching + [{"verdict": "fail"}] * (doc["cases"] - matching)
                args = ["--min-pass", str(run["min_pass"])] if "min_pass" in run else []
                rc, out, _ = self.golden(*args, fixtures=Path(tmp), role=doc["role"])
                passed = sum(v == "PASS" for v in self.rows(out).values())
                got = {"exit": rc, "passed": passed, "failed": doc["cases"] - passed}
                self.assertEqual(got, expect[name], name)

    def test_empty_golden_set_is_error_not_pass(self):
        doc, expect = self.row("empty-golden-set-is-error-not-pass")
        self.write_config(_route("m1"))
        with tempfile.TemporaryDirectory() as tmp:
            roots = {"missing": Path(tmp) / "no-such-dir", "empty": Path(tmp) / "empty",
                     "no_case_json": Path(tmp) / "bare"}
            (roots["empty"] / doc["role"]).mkdir(parents=True)
            for sub in ("01", "02"):
                (roots["no_case_json"] / doc["role"] / sub).mkdir(parents=True)
            self.assertEqual(set(roots), set(doc["cases_dirs"]))
            for name, root in roots.items():
                rc, out, _ = self.golden("--min-pass", str(doc["min_pass"]), fixtures=root,
                                         role=doc["role"])
                self.assertEqual({"exit": rc}, expect[name], name)
                self.assertNotIn("PASS", out)

    def test_swap_is_one_routing_line(self):
        doc, expect = self.row("swap-is-one-routing-line")
        role = doc["role"]

        def write(spec):
            cfg = _config(_route("unused"))
            cfg["routing"][role] = {**cfg["routing"][role], **spec["routing"][role]}
            (self.home / "config.json").write_text(json.dumps(cfg))
            return cfg

        cfg_a, cfg_b = write(doc["config_a"]), write(doc["config_b"])
        routes_b = cfg_b["routing"]
        differing = [f"routing.{r}" for r, a in cfg_a["routing"].items() if a != routes_b[r]]
        self.assertEqual(differing, expect["changed_config_paths"])
        used, passes = {}, []
        with tempfile.TemporaryDirectory() as tmp:
            _cases(tmp, role, 2)
            for name, args in (("config_a", []), ("config_b", []),
                               ("route_flag_run", ["--route", doc["route_flag_run"]["route"]])):
                write(doc["config_b" if name == "config_b" else "config_a"])
                before = (self.home / "config.json").read_text()
                self.reports = [PASS, PASS]
                rc, _, _ = self.golden(*args, fixtures=Path(tmp), role=role)
                route = self.seen[-1]["route"]
                used[name] = f"{route['provider']}/{route['model']}"
                passes.append(rc == 0)
                if name == "route_flag_run":
                    self.assertEqual((self.home / "config.json").read_text() != before,
                                     expect["config_a_changed_by_route_flag_run"])
        self.assertEqual(used, expect["adapter_used"])
        self.assertEqual(all(passes), expect["golden_passes_on_both"])
        literals = re.compile(r"claude-|opus|sonnet|fable|haiku", re.I)
        loop = [paths.REPO / "capsule" / "golden.py", paths.REPO / "capsule" / "golden_cli.py",
                *(paths.REPO / "contracts").glob("*.md")]
        self.assertEqual(sum(len(literals.findall(p.read_text())) for p in loop),
                         expect["provider_or_model_literals_in_loop_or_contracts"])


if __name__ == "__main__":
    unittest.main()
