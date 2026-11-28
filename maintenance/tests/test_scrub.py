import io
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scrub

SCRUB_PY = Path(__file__).resolve().parents[1] / "scrub.py"
SCRUB_LUA = Path(__file__).resolve().parents[2] / "observability" / "fluent-bit" / "scrub.lua"
# lua5.4 is what CI installs (`sudo apt-get install -y lua5.4` in
# .github/workflows/verify.yml, asserted by tests/test_ci_yaml.sh); Debian/
# Ubuntu ship the binary under that versioned name only, so look for it
# alongside the plain `lua`/`luajit` a laptop is likelier to have.
LUA_BIN = shutil.which("lua") or shutil.which("lua5.4") or shutil.which("luajit")

# Fixture lines shared by the Lua parity/sync tests below - one per kind
# scrub.py redacts, plus the negative fixtures from the Scrub tests above,
# so both directions (redacted / left alone) are checked against scrub.lua.
LUA_FIXTURES = [
    ("telegram-token", "bot token is 123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA in the config"),
    ("openai-key", "key=sk-" + "a" * 24 + " used"),
    ("github-token (ghp)", "push with ghp_" + "b" * 36 + " please"),
    ("github-token (pat)", "token: github_pat_" + "c" * 40),
    ("github-token (gho)", "x gho_" + "x" * 36 + " y"),
    ("github-token (ghs)", "x ghs_" + "x" * 36 + " y"),
    ("github-token (ghu)", "x ghu_" + "x" * 36 + " y"),
    ("github-token (ghr)", "x ghr_" + "x" * 36 + " y"),
    ("aws-key", "id AKIA" + "X" * 16 + " ok"),
    ("slack-token", "hook xoxb-" + "1" * 12 + " ok"),
    ("google-key", "key AIza" + "x" * 35 + " ok"),
    ("private-key",
     "-----BEGIN RSA PRIVATE" + " KEY----- body -----END RSA PRIVATE" + " KEY----- tail"),
    ("private-key unterminated",
     "x -----BEGIN RSA PRIVATE" + " KEY----- MIIEvQIBADANBgkq"),
    ("colon kv", "password: " + "hunter2hunter2"),
    ("colon json kv", '{"api_key": "' + "x" * 8 + '", "n": 1}'),
    ("colon json token", '{"access_token": "' + "x" * 8 + '"}'),
    ("home path", "reading /Users/" + "alice/notes.txt now"),
    ("colon after level", "WARN: password: " + "hunter2hunter2"),
    ("colon nested key", "user: password: " + "hunter2hunter2"),
    ("colon key then token", "key: token: " + "hunter2hunter2"),
    ("colon glued token", "level:token:" + "hunter2hunter2"),
    ("colon url token", "http://h:8080/token: " + "hunter2hunter2"),
    ("home path anchor negative", "/api/Users/42/x and /private/Users/x/"),
    ("colon negative", "token bucket refills: slowly"),
    ("colon short negative", "password: abc"),
    ("password prose negative", "the password field is empty"),
    ("jwt", "auth=eyJ" + "d" * 10 + "." + "e" * 10 + "." + "f" * 10 + " sent"),
    ("client_secret kv", "client_secret=abc123 in env"),
    ("password kv", "password=hunter2"),
    ("api_key kv", "api_key=XYZ"),
    ("hyphenated api-key kv (prefix kept)", "x-api-key=abcdefghij"),
    ("hyphenated token kv (prefix kept)", "auth-token=klmnopqrst"),
    # I1: env-var-shaped keys - the whole key is kept, the value goes.
    ("env-var client_secret kv", "INFISICAL_CLIENT_SECRET=abc"),
    ("env-var token kv", "TELEGRAM_BOT_TOKEN=xyz"),
    ("env-var api_key kv", "LEGACY_API_KEY=k"),
    ("infisical", "universal auth st." + "a1b2c3d4e5" * 3 + " rotated"),
    ("telegram negative", "telegram:-1001234567890:10"),
    ("task id negative", "t_deadbeef"),
    ("sha256 negative", "a" * 64),
    ("ledger negative", "ledger pushed: 3 entries"),
    ("iso timestamp negative", "2026-09-09T12:34:56Z"),
    ("pr url negative", "https://github.com/example-owner/sample-board/pull/5"),
    ("ops.env test command negative", "OPS_TEST_CMD_capsule_corp='python3 -m unittest'"),
    # C1: ordinary log text the Lua side used to redact for want of the
    # %f[%w_] frontier ("sk-"/"st." mid-identifier) or a length floor.
    ("kanban word negative (sk-)", "task-list"),
    ("english word negative (sk-)", "risk-free"),
    ("metric name negative (sk.)", "capsule.host.disk.pct"),
    ("docker host negative (st.)", "host.docker.internal"),
    ("db filename negative (st.)", "test.db"),
    ("hex-looking suffix negative (st.)", "manifest.cafe"),
    ("short hex suffix negative (st.)", "latest.bad"),
]

# The literal Lua pattern strings scrub.lua's header documents and its code
# uses, keyed by the same kind names as scrub.PATTERNS - checked against the
# file when no lua/luajit binary is available to run scrub.lua for real (see
# LuaSourceSync below).
# Every non-kv row carries the %f[%w_] left frontier (Lua's \b, C1) and its
# own minimum-length floor, written with scrub.lua's `min(class, n)` helper
# (Lua has no {n,} quantifier); the kv row carries the frontier plus the
# `[%w_]*` env-var-shaped-key prefix (I1). The one exception is
# telegram-token: scrub.py's anchor there is `(?<![\w:-])`, a negative
# lookbehind over a set that also excludes ":" and "-", which no Lua pattern
# can express - so that row is deliberately unanchored and its floor alone
# keeps the "telegram negative" fixture out. The residual divergence is
# over-redaction only (Lua may flag a run scrub.py's lookbehind lets
# through, never the reverse), which is why no frontier string is listed for
# it below.
LUA_PATTERN_STRINGS = {
    "telegram-token": ["%d%d%d%d%d%d+:", 'min("[%w_%-]", 20)'],
    "openai-key": ["%f[%w_]sk%-", 'min("[%w_%-]", 20)'],
    "github-token": ["%f[%w_]ghp_", 'min("[%w]", 30)', "%f[%w_]github_pat_", 'min("[%w_]", 30)'],
    "jwt": ["%f[%w_]eyJ", 'min("[%w_%-]", 10)'],
    "infisical": ["%f[%w_]st%.", 'min("[%x%-]", 20)'],
    "kv-secret": ["%f[%w_]([%w_]*", "client_secret", "password", "passwd", "token", "api_key", "api-key", "apikey", "secret"],
    "aws-key": ["%f[%w_]A[KS]IA", "%f[^%w_]"],
    "slack-token": ["%f[%w_]xox[abprs]%-", "%f[%w_]xapp%-", 'min("[%w%-]", 10)'],
    "google-key": ["%f[%w_]AIza", 'string.rep("[%w_%-]", 35)'],
    "private-key": ["BEGIN [%u ]*PRIVATE KEY", "END [%u ]*PRIVATE KEY"],
    "kv-colon": ["COLON_WORDS", "COLON_QUOTED", "COLON_BARE"],
}

# A tiny Lua harness that loads scrub.lua by path (argv[1]) and runs
# scrub_log over one line of text (argv[2]), printing the redacted log or
# the literal string UNCHANGED when scrub_log reported code 0.
LUA_HARNESS = """
dofile(arg[1])
local code, _, record = scrub_log("t", 0, { log = arg[2] })
if code == 0 then
  io.write("UNCHANGED")
else
  io.write(record["log"])
end
"""


# 100 KB adversarial lines for the PEM and api_key patterns (Python and Lua
# must each handle them in under 0.5 s).
_PEM_HEAD = "-----BEGIN PRIVATE" + " KEY-----"
_PEM_API_SHAPES = [
    _PEM_HEAD * 3700,
    (_PEM_HEAD + "\n") * 3700,
    ("-----END PRIVATE" + " KEY-----") * 4000,
    "api_key" * 14000,
    "-----BEGIN " + "A" * 100000,
    _PEM_HEAD + "A" * 100000,
]


class Scrub(unittest.TestCase):
    def test_non_str_and_empty_unchanged(self):
        self.assertIsNone(scrub.scrub(None))
        self.assertEqual(scrub.scrub(""), "")
        self.assertEqual(scrub.scrub(42), 42)
        self.assertEqual(scrub.scrub([1, 2]), [1, 2])

    def test_telegram_token_redacted(self):
        value = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        text = f"bot token is {value} in the config"
        out = scrub.scrub(text)
        self.assertNotIn(value, out)
        self.assertIn("<redacted:telegram-token>", out)
        self.assertTrue(out.startswith("bot token is <redacted:telegram-token> in the config"))

    def test_openai_key_redacted(self):
        value = "sk-" + "a" * 24
        text = f"key={value} used"
        out = scrub.scrub(text)
        self.assertNotIn(value, out)
        self.assertIn("<redacted:openai-key>", out)

    def test_github_token_ghp_redacted(self):
        value = "ghp_" + "b" * 36
        out = scrub.scrub(f"push with {value} please")
        self.assertNotIn(value, out)
        self.assertIn("<redacted:github-token>", out)
        self.assertTrue(out.startswith("push with <redacted:github-token>"))

    def test_github_token_pat_redacted(self):
        value = "github_pat_" + "c" * 40
        out = scrub.scrub(f"token: {value}")
        self.assertNotIn(value, out)
        self.assertIn("<redacted:github-token>", out)

    def test_jwt_redacted(self):
        value = "eyJ" + "d" * 10 + "." + "e" * 10 + "." + "f" * 10
        out = scrub.scrub(f"auth={value} sent")
        self.assertNotIn(value, out)
        self.assertIn("<redacted:jwt>", out)

    def test_client_secret_kv_redacted_key_kept(self):
        out = scrub.scrub("client_secret=abc123 in env")
        self.assertNotIn("abc123", out)
        self.assertEqual(out, "client_secret=<redacted:kv-secret> in env")

    def test_password_kv_redacted_key_kept(self):
        out = scrub.scrub("password=hunter2")
        self.assertEqual(out, "password=<redacted:kv-secret>")

    def test_api_key_kv_redacted_key_kept(self):
        out = scrub.scrub("api_key=XYZ")
        self.assertEqual(out, "api_key=<redacted:kv-secret>")

    def test_env_var_shaped_keys_redacted_whole_key_kept(self):
        """I1: the review's deferred T1 note - `SOMETHING_TOKEN=value` keys
        were NOT matched (the old `\\b` refused to fire mid-identifier), so
        an env dump reached Postgres/the ledger with live values. The key,
        prefix included, is kept; only the value goes."""
        self.assertEqual(scrub.scrub("INFISICAL_CLIENT_SECRET=abc"),
                         "INFISICAL_CLIENT_SECRET=<redacted:kv-secret>")
        self.assertEqual(scrub.scrub("TELEGRAM_BOT_TOKEN=xyz"),
                         "TELEGRAM_BOT_TOKEN=<redacted:kv-secret>")
        self.assertEqual(scrub.scrub("LEGACY_API_KEY=k"),
                         "LEGACY_API_KEY=<redacted:kv-secret>")
        self.assertEqual(scrub.contains_secret("TELEGRAM_BOT_TOKEN=xyz"), "kv-secret")

    def test_lowercase_word_prefix_also_redacts_known_cost(self):
        """The documented cost of the `[A-Za-z0-9_]*` prefix: an English word
        ending in a key name redacts too. Over-redaction is the cheap
        direction; pinned here so the behaviour is deliberate, not a
        surprise."""
        self.assertEqual(scrub.scrub("mypassword=x"), "mypassword=<redacted:kv-secret>")

    def test_ops_env_test_command_negative_survives(self):
        text = "OPS_TEST_CMD_capsule_corp='python3 -m unittest'"
        self.assertEqual(scrub.scrub(text), text)
        self.assertIsNone(scrub.contains_secret(text))

    def test_ordinary_words_and_hostnames_survive(self):
        """C1: the Lua side redacted these for want of a `\\b` frontier and a
        length floor ("ri[sk-]free", "ho[st.d]ocker.internal" - [%x] accepts
        "d"). scrub.py always left them alone; pinned on both sides now."""
        for text in ("task-list", "risk-free", "capsule.host.disk.pct",
                     "host.docker.internal", "test.db", "manifest.cafe",
                     "latest.bad"):
            self.assertEqual(scrub.scrub(text), text)
            self.assertIsNone(scrub.contains_secret(text))

    def test_infisical_token_redacted(self):
        value = "st." + "a1b2c3d4e5" * 3
        out = scrub.scrub(f"universal auth {value} rotated")
        self.assertNotIn(value, out)
        self.assertIn("<redacted:infisical>", out)

    def test_telegram_negative_survives(self):
        text = "telegram:-1001234567890:10"
        self.assertEqual(scrub.scrub(text), text)
        self.assertIsNone(scrub.contains_secret(text))

    def test_task_id_negative_survives(self):
        text = "t_deadbeef"
        self.assertEqual(scrub.scrub(text), text)
        self.assertIsNone(scrub.contains_secret(text))

    def test_sha256_negative_survives(self):
        text = "a" * 64
        self.assertEqual(scrub.scrub(text), text)
        self.assertIsNone(scrub.contains_secret(text))

    def test_ledger_message_negative_survives(self):
        text = "ledger pushed: 3 entries"
        self.assertEqual(scrub.scrub(text), text)
        self.assertIsNone(scrub.contains_secret(text))

    def test_iso_timestamp_negative_survives(self):
        text = "2026-09-09T12:34:56Z"
        self.assertEqual(scrub.scrub(text), text)
        self.assertIsNone(scrub.contains_secret(text))

    def test_pr_url_negative_survives(self):
        text = "https://github.com/example-owner/sample-board/pull/5"
        self.assertEqual(scrub.scrub(text), text)
        self.assertIsNone(scrub.contains_secret(text))


class ScrubWiden(unittest.TestCase):
    """W6-4: more secret shapes. Samples are built by concatenation so no
    literal token sits in this source for the repo's own scanners to hit."""

    def _gone(self, value, kind):
        out = scrub.scrub(f"see {value} here")
        self.assertNotIn(value, out)
        self.assertIn(f"<redacted:{kind}>", out)
        self.assertEqual(scrub.contains_secret(value), kind)

    def test_github_token_prefixes(self):
        for prefix in ("ghp_", "gho_", "ghu_", "ghs_", "ghr_"):
            self._gone(prefix + "x" * 36, "github-token")

    def test_aws_key(self):
        self._gone("AKIA" + "X" * 16, "aws-key")
        self.assertEqual(scrub.scrub("AKIA" + "X" * 15), "AKIA" + "X" * 15)
        self._gone("ASIA" + "X" * 16, "aws-key")
        self.assertEqual(scrub.scrub("ASIAX"), "ASIAX")
        self.assertEqual(scrub.scrub("ghost_" + "x" * 36), "ghost_" + "x" * 36)

    def test_slack_token(self):
        for letter in "abprs":
            self._gone("xox" + letter + "-" + "1" * 12, "slack-token")
        self._gone("xapp-" + "1" * 12, "slack-token")
        self.assertEqual(scrub.scrub("xapp-1"), "xapp-1")

    def test_google_key(self):
        self._gone("AIza" + "x" * 35, "google-key")

    def test_private_key_block(self):
        head = "-----BEGIN OPENSSH PRIVATE" + " KEY-----"
        tail = "-----END OPENSSH PRIVATE" + " KEY-----"
        out = scrub.scrub(f"a\n{head}\nAAAA\nBBBB\n{tail}\nb")
        self.assertEqual(out, "a\n<redacted:private-key>\nb")

    def test_colon_forms(self):
        self.assertEqual(scrub.scrub("password: " + "hunter2hunter2"),
                         "password: <redacted:kv-secret>")
        self.assertEqual(scrub.scrub('{"api_key": "' + "x" * 8 + '", "n": 1}'),
                         '{"api_key": "<redacted:kv-secret>", "n": 1}')
        self.assertEqual(scrub.scrub('{"client_secret":"' + "x" * 8 + '"}'),
                         '{"client_secret":"<redacted:kv-secret>"}')

    def test_ordinary_text_untouched(self):
        for text in ("token bucket refills: slowly", "password: abc", "the password field is empty",
                     "a token bucket algorithm", "secret sauce"):
            self.assertEqual(scrub.scrub(text), text)
            self.assertIsNone(scrub.contains_secret(text))

    def test_home_paths_collapse(self):
        self.assertEqual(scrub.scrub("at /Users/" + "alice/code/x.py ok"), "at ~/code/x.py ok")
        self.assertEqual(scrub.scrub("at ~/code/x.py ok"), "at ~/code/x.py ok")

    def test_home_path_left_anchor(self):
        for text in ("/api/Users/42/x", "/private/Users/x/y", "a.b/Users/x/y"):
            self.assertEqual(scrub.scrub(text), text)
        self.assertEqual(scrub.scrub("/Users/" + "bob/x"), "~/x")

    def test_adversarial_lines_are_linear(self):
        lines = ["token" * 20000, "password-" * 10000, "a" * 100000, "secret_" * 15000 + "x",
                 '"' + "api_key" * 14000, "ghp_" * 25000, "sk-" * 30000, "AKIA" * 25000,
                 "xox" * 30000, "AIza" * 25000, "/Users/" * 14000, "a:" * 50000,
                 "token: " * 14000, "eyJ" + "a" * 100000]
        for line in lines:
            for fn in (scrub.scrub, scrub.contains_secret):
                start = time.monotonic()
                fn(line)
                self.assertLess(time.monotonic() - start, 0.5, f"{fn.__name__} {line[:12]!r}")

    def test_pem_and_api_key_shapes_are_linear(self):
        for line in _PEM_API_SHAPES:
            for fn in (scrub.scrub, scrub.contains_secret):
                start = time.monotonic()
                fn(line)
                self.assertLess(time.monotonic() - start, 0.5, f"{fn.__name__} {line[:14]!r}")

    def test_unterminated_pem_begin_is_redacted_to_end(self):
        text = "x -----BEGIN PRIVATE" + " KEY----- body"
        self.assertEqual(scrub.scrub(text), "x <redacted:private-key>")
        two = "-----BEGIN A PRIVATE" + " KEY-----k-----END A PRIVATE" + " KEY----- ok " + text
        self.assertEqual(scrub.scrub(two), "<redacted:private-key> ok x <redacted:private-key>")

    def test_already_redacted_not_rewrapped(self):
        text = "token: <redacted:github-token>"
        self.assertEqual(scrub.scrub(text), text)


class ContainsSecret(unittest.TestCase):
    def test_returns_kind_on_hit(self):
        self.assertEqual(scrub.contains_secret("password=hunter2"), "kv-secret")
        self.assertEqual(
            scrub.contains_secret("123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
            "telegram-token",
        )

    def test_returns_none_on_clean_text(self):
        self.assertIsNone(scrub.contains_secret("just a normal sentence"))
        self.assertIsNone(scrub.contains_secret(""))
        self.assertIsNone(scrub.contains_secret(None))


class CheckCli(unittest.TestCase):
    def _run(self, stdin_text):
        return subprocess.run(
            [sys.executable, str(SCRUB_PY), "--check"],
            input=stdin_text, capture_output=True, text=True,
        )

    def test_exit_1_and_reports_kind_and_line_never_value(self):
        stdin_text = "clean line one\npassword=hunter2\nclean line three\n"
        result = self._run(stdin_text)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("hunter2", result.stdout)
        self.assertIn("scrub: kv-secret at line 2", result.stdout)

    def test_exit_1_on_pattern_hit_without_kv(self):
        value = "ghp_" + "z" * 36
        result = self._run(f"{value}\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("scrub: github-token at line 1", result.stdout)

    def test_exit_0_on_clean_input(self):
        result = self._run("telegram:-1001234567890:10\nledger pushed: 3 entries\n")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")


class LuaSourceSync(unittest.TestCase):
    """scrub.lua (Fluent Bit) reimplements scrub.py's PATTERNS by hand; these
    two tests are how the two lists are kept from silently diverging."""

    def test_pattern_kind_order_matches_scrub_lua_header_order(self):
        kinds = [kind for kind, _ in scrub.PATTERNS]
        self.assertEqual(
            kinds,
            ["telegram-token", "openai-key", "github-token", "jwt", "infisical", "aws-key",
             "slack-token", "google-key", "private-key", "kv-secret", "kv-colon"],
        )

    @unittest.skipIf(LUA_BIN, "lua/luajit present - LuaParity below exercises scrub.lua directly")
    def test_header_lists_every_kind_in_order_with_its_pattern(self):
        text = SCRUB_LUA.read_text()
        kinds = [kind for kind, _ in scrub.PATTERNS]
        idx = 0
        for kind in kinds:
            pos = text.index(kind, idx)
            self.assertGreaterEqual(pos, idx, f"{kind!r} out of order in scrub.lua header")
            idx = pos + len(kind)
        for kind in kinds:
            for pattern in LUA_PATTERN_STRINGS[kind]:
                self.assertIn(pattern, text, f"scrub.lua is missing the {kind} pattern {pattern!r}")


@unittest.skipUnless(LUA_BIN, "no lua/luajit binary on PATH")
class LuaParity(unittest.TestCase):
    """When a Lua interpreter is available, run every fixture line through
    scrub.lua for real and require the same redaction scrub.py produces."""

    @classmethod
    def setUpClass(cls):
        cls._harness = tempfile.NamedTemporaryFile(mode="w", suffix=".lua", delete=False)
        cls._harness.write(LUA_HARNESS)
        cls._harness.close()

    @classmethod
    def tearDownClass(cls):
        Path(cls._harness.name).unlink(missing_ok=True)

    def _run_lua(self, text):
        result = subprocess.run(
            [LUA_BIN, self._harness.name, str(SCRUB_LUA), text],
            capture_output=True, text=True, check=True,
        )
        return result.stdout

    def test_pem_and_api_key_shapes_are_linear_in_lua(self):
        for line in _PEM_API_SHAPES:
            start = time.monotonic()
            self._run_lua(line)
            self.assertLess(time.monotonic() - start, 0.5, f"lua {line[:14]!r}")

    def test_fixtures_match_python_redaction(self):
        for name, text in LUA_FIXTURES:
            expected = scrub.scrub(text)
            got = self._run_lua(text)
            if got == "UNCHANGED":
                got = text
            self.assertEqual(got, expected, f"lua/python mismatch for {name}: {text!r}")


if __name__ == "__main__":
    unittest.main()
