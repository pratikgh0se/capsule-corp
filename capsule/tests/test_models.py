# mutation-check: targets-predate-range b957739c9 -- names out of tests, no behaviour pinned ::
#   ClaudeEnv.test_capsule_worker_env_marker_is_set
import json, os, shutil, tempfile, time, unittest
from pathlib import Path
from capsule import golden, paths
from capsule.runners import docker, host
from capsule.models import Result, with_retries, claude, stub

FX = Path(__file__).parent / "fixtures" / "claude"
ROUTE = {"provider": "claude", "account": "default", "model": "claude-sonnet-5", "fallback": None, "turn_budget": 80}

def res(error_class=None, attempts=1):
    return Result(None, 1 if error_class else 0, {}, attempts, error_class, None, None, "")

class Parse(unittest.TestCase):
    def test_ok_reads_report_and_tokens(self):
        r = claude.parse((FX / "ok.json").read_text(), "", 0)
        self.assertEqual((r.error_class, r.report), (None, {"a": 1}))
        self.assertTrue(r.tokens["in"] > 0 and r.tokens["out"] > 0)

    def test_max_turns(self):
        self.assertEqual(claude.parse((FX / "max_turns.json").read_text(), "", 1).error_class, "max_turns")

    def test_rate_limit_has_window(self):
        r = claude.parse((FX / "limit.txt").read_text(), "", 1, now=1_700_000_000)
        self.assertEqual(r.error_class, "rate_limit"); self.assertGreater(r.retry_after, 1_700_000_000)

    def test_rate_limit_retry_after_is_pinned_to_the_parsed_reset_hour(self):
        """`_retry_after` reads `now` through `datetime.fromtimestamp`, i.e. local time (task-7
        review item 5) - TZ is pinned for the duration of the test so the expected timestamp is
        reproducible everywhere, and the fixture's "resets 3pm" is checked against an exact
        value rather than a loose `assertGreater` (R28: clock injection needs a pinned test)."""
        old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        self.addCleanup(self._restore_tz, old_tz)
        now = 1_732_000_000   # 2024-11-19 07:06:40 UTC
        r = claude.parse((FX / "limit.txt").read_text(), "", 1, now=now)
        self.assertEqual(r.error_class, "rate_limit")
        self.assertEqual(r.retry_after, 1_732_028_400)   # 2024-11-19 15:00:00 UTC ("resets 3pm")

    @staticmethod
    def _restore_tz(old_tz):
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()

    def test_overloaded_is_transient(self):
        self.assertEqual(claude.parse((FX / "overloaded.json").read_text(), "", 1).error_class, "transient")

    def test_logged_out_is_fatal_not_transient(self):
        self.assertEqual(claude.parse("", (FX / "logged_out.txt").read_text(), 1).error_class, "fatal")

    def test_empty_stdout_rc_zero_is_fatal(self):
        # Fix round 1, Important #3: unparsable/empty stdout is always fatal,
        # even with a clean exit code - never a guessed success.
        self.assertEqual(claude.parse("", "", 0).error_class, "fatal")

    def test_ok_report_mentioning_rate_limit_stays_ok(self):
        # Fix round 1, Important #1: a valid, successful report that happens
        # to mention "rate limit" (e.g. this very task's own work) must never
        # be misclassified as a rate_limit hit.
        obj = {
            "type": "result", "subtype": "success", "is_error": False,
            "session_id": "00000000-0000-0000-0000-000000000000",
            "result": json.dumps({"note": "added rate limit handling"}),
            "structured_output": {"note": "added rate limit handling"},
            "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0},
        }
        r = claude.parse(json.dumps(obj), "", 0)
        self.assertIsNone(r.error_class)

    def test_transient_regex_ignores_digits_elsewhere_in_envelope(self):
        # Fix round 1, Important #2: a digit-heavy session_id/duration must
        # never make a genuinely fatal error look transient.
        obj = {
            "type": "result", "subtype": "error_during_execution", "is_error": True,
            "session_id": "9f1c2d57-5a10-4e21-b555-529456789a",
            "duration_api_ms": 1523,
            "result": "Invalid tool config",
            "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0},
        }
        r = claude.parse(json.dumps(obj), "", 1)
        self.assertEqual(r.error_class, "fatal")

class Argv(unittest.TestCase):
    def test_flags(self):
        argv = claude.build_argv(ROUTE, {"system": "SOUL", "prompt": "p"}, "/w", {"schema": {"type": "object"}, "tools": ""})
        for flag in ("-p", "--restricted", "--strict-mcp-config", "--json-schema", "--output-format"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--max-turns") + 1], "80")
        self.assertNotIn("bypassPermissions", argv)

    def test_reasoning_effort_reaches_the_cli_only_when_set(self):
        limits = {"schema": {"type": "object"}, "tools": ""}
        bundle = {"system": "S", "prompt": "p"}
        argv = claude.build_argv(dict(ROUTE, reasoning_effort="high"), bundle, "/w", limits)
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertNotIn("--effort", claude.build_argv(ROUTE, bundle, "/w", limits))

class RunnerLimitsToArgv(unittest.TestCase):
    """Final review C2/I2: the seam StrongRunner/HostRunner.limits -> build_argv. `dontAsk`
    plus `--restricted` denies every tool that is not pre-approved, so a writer needs
    run.sh's shape: acceptEdits plus --allowedTools for the MCP tool."""

    def setUp(self):
        self.sp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.sp, True)
        (self.sp / "requests").mkdir()

    def argv(self, limits):
        return claude.build_argv(ROUTE, {"system": "S", "prompt": "p"}, "/w", dict(limits, schema={"type": "object"}))

    @staticmethod
    def val(argv, flag):
        return argv[argv.index(flag) + 1]

    def test_implementer_gets_edits_and_exec(self):
        argv = self.argv(docker.StrongRunner().limits("capsule-abc", self.sp))
        self.assertEqual(self.val(argv, "--permission-mode"), "acceptEdits")
        self.assertNotIn("dontAsk", argv)
        self.assertIn("mcp__capsule__exec", self.val(argv, "--allowedTools").split(","))
        tools = self.val(argv, "--tools").split(",")
        self.assertIn("Edit", tools)
        self.assertIn("Write", tools)
        self.assertFalse([t for t in tools if t.startswith("mcp__")], tools)   # --tools: built-ins only
        self.assertEqual(self.val(argv, "--add-dir"), str(self.sp / "requests"))
        self.assertEqual(self.val(argv, "--mcp-config"), str(self.sp / "mcp.json"))

    def test_review_roles_stay_read_only_but_can_file_a_request(self):
        requests = self.sp / "requests"
        for role, base in (("reviewer", {"Read", "Glob", "Grep"}), ("validator", set()), ("guide", set())):
            argv = self.argv(host.HostRunner().limits(role, self.sp))
            self.assertEqual(self.val(argv, "--permission-mode"), "dontAsk", role)
            self.assertEqual(set(self.val(argv, "--tools").split(",")) - {""}, base | {"Write"}, role)
            self.assertNotIn("mcp__capsule__exec", " ".join(argv), role)
            # the only write any review role is pre-approved for is its own requests/ dir
            self.assertEqual(self.val(argv, "--allowedTools"), f"Edit(/{requests}/**)", role)
            self.assertEqual(self.val(argv, "--add-dir"), str(requests), role)


class ExampleConfig(unittest.TestCase):
    def test_every_role_has_room_for_a_structured_output_turn(self):
        """Final review I1: structured output arrives through a tool turn (fixtures/claude/ok.json:
        num_turns 2 for a trivial report), so a 1-turn budget ends every run `error_max_turns`."""
        self.assertEqual(json.loads((FX / "ok.json").read_text())["num_turns"], 2)
        cfg = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        for role, route in cfg["routing"].items():
            self.assertGreaterEqual(route["turn_budget"], 3, role)


class ClaudeEnv(unittest.TestCase):
    def test_git_the_cli_runs_is_pinned_and_hardened(self):
        """Final review C1: any git the host `claude` process runs uses the tick's git dir, with
        hooks and fsmonitor off, never whatever `.git` sits in the workspace."""
        env = claude._clean_env({"git_dir": "/gd"}, "/w")
        self.assertEqual((env["GIT_DIR"], env["GIT_WORK_TREE"]), ("/gd", "/w"))
        n = int(env["GIT_CONFIG_COUNT"])
        pairs = {(env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(n)}
        self.assertIn(("core.hooksPath", "/dev/null"), pairs)
        self.assertIn(("core.fsmonitor", "false"), pairs)

    def test_capsule_worker_env_marker_is_set(self):
        """Row 470 fix round 1, I6: a Claude Code user-level hook (capsule unblocked's
        SessionStart/Stop) must be able to tell a dispatched worker's own process apart from
        the owner's interactive session, purely from its env."""
        self.assertEqual(claude._clean_env()["CAPSULE_WORKER"], "1")


class Retries(unittest.TestCase):
    def test_two_transients_then_ok(self):
        seq = [res("transient"), res("transient"), res()]
        r = with_retries(lambda: seq.pop(0), sleep=lambda s: None)
        self.assertEqual((r.error_class, r.attempts), (None, 3))

    def test_gives_up_after_three_retries(self):
        calls = []
        r = with_retries(lambda: calls.append(1) or res("transient"), sleep=lambda s: None)
        self.assertEqual((r.error_class, len(calls), r.attempts), ("transient", 4, 4))

    def test_rate_limit_not_retried(self):
        calls = []
        r = with_retries(lambda: calls.append(1) or res("rate_limit"), sleep=lambda s: None)
        self.assertEqual((r.error_class, len(calls)), ("rate_limit", 1))

class SwapAndGolden(unittest.TestCase):
    def test_stub_swap_is_one_routing_line(self):
        a = stub.StubAdapter([{"x": 1}])
        self.assertEqual(a.run("validator", {}, "/w", {}).report, {"x": 1})

    def test_golden_stub_counts(self):
        cases = Path(__file__).parent / "fixtures" / "golden" / "validator"
        out = golden.run("validator", cases, stub.StubAdapter([{"verdict": "pass"}, {"verdict": "fail"}]))
        self.assertEqual((out["passed"], out["failed"]), (1, 1))


class CliSchemaTests(unittest.TestCase):
    def test_json_schema_arg_drops_meta_keys(self):
        # live smoke 2026-09-26: `claude -p --json-schema` rejects a schema carrying
        # the draft-2020-12 `$schema` URI, so the argv carries the structural schema only
        from capsule.models import claude as c
        import json as _j
        limits = {"schema": {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "x", "type": "object"}}
        argv = c.build_argv(dict(ROUTE), {"system": "s", "prompt": "p"}, "/tmp/w", limits)
        sent = _j.loads(argv[argv.index("--json-schema") + 1])
        self.assertEqual(sent, {"type": "object"})
