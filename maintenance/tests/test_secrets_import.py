# mutation-check: targets-predate-range b957739c9 -- rename only, no behaviour pinned ::
#   ReadSources.test_no_legacy_telegram_source
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import secrets_import as si
from capsule.tests.private_names import retired_only  # noqa: E402
os.environ.setdefault("CAPSULE_ADMIN_EMAIL", "admin@example.com")  # no shipped default


VERIFY_SECRET = "pgpw123"


class FakeApi:
    """Fakes _api by (method, path). Keeps enough state across calls for
    two runs to behave like a real idempotent server.

    Scripted failures: call `script_failure(method, path, exc=None,
    times=1)` to make the next N calls to that exact (method, path) raise
    `exc` (default a generic RuntimeError) instead of touching the fake
    state. Consumed in FIFO order per key.
    """

    def __init__(self):
        self.calls = []
        self.projects = {}          # slug -> id
        self.project_envs = {}      # id -> set(env slugs)
        self.identities = {}        # name -> id
        self.memberships = set()    # (project_id, identity_id)
        self.secrets = {}           # (project_id, name) -> value
        self.status_ok = True       # GET /status answers unless overridden
        self.failures = {}          # (method, path) -> [exc, ...] FIFO
        self.logins = []            # bodies of every universal-auth login call
        self.valid_admin_client_secret = None  # None = any client secret logs in ok

    def script_failure(self, method, path, exc=None, times=1):
        key = (method, path)
        queue = self.failures.setdefault(key, [])
        for _ in range(times):
            queue.append(exc or RuntimeError(f"{method} {path}: HTTP 500 scripted failure"))

    def __call__(self, method, path, body=None, token=None, base=None):
        self.calls.append((method, path))
        if path not in ("/status", "/v1/auth/universal-auth/login"):
            assert token, "every call after bootstrap must carry a token"
        assert base, "every call must carry a base"

        queue = self.failures.get((method, path))
        if queue:
            raise queue.pop(0)

        if method == "GET" and path == "/status":
            if not self.status_ok:
                raise RuntimeError(f"GET {path}: connection refused")
            return {"date": "2026-09-07T00:00:00Z"}

        if method == "GET" and path.startswith("/v2/workspace/") and "/identity-memberships" not in path:
            slug = path.split("/")[-1]
            if slug not in self.projects:
                raise si.ApiError(f"GET {path}: HTTP 404 not found", 404)
            pid = self.projects[slug]
            envs = [{"slug": s, "name": s, "id": f"env-{s}"} for s in self.project_envs.get(pid, set())]
            return {"id": pid, "environments": envs}

        if method == "POST" and path == "/v2/workspace":
            slug = body["slug"]
            pid = f"proj-{slug}"
            self.projects[slug] = pid
            self.project_envs[pid] = set()
            return {"id": pid, "environments": []}

        if method == "POST" and path.startswith("/v1/workspace/") and path.endswith("/environments"):
            pid = path.split("/")[3]
            self.project_envs.setdefault(pid, set()).add(body["slug"])
            return {}

        if method == "GET" and "/identity-memberships" in path and "/v2/organizations/" in path:
            return {
                "identityMemberships": [
                    {"identityId": iid, "identity": {"id": iid, "name": name}}
                    for name, iid in self.identities.items()
                ],
                "totalCount": len(self.identities),
            }

        if method == "POST" and path == "/v1/identities":
            name = body["name"]
            iid = f"id-{name}"
            self.identities[name] = iid
            return {"identity": {"id": iid, "name": name}}

        if method == "POST" and path.startswith("/v1/auth/universal-auth/identities/") and path.endswith("/client-secrets"):
            iid = path.split("/")[-2]
            return {"clientSecret": f"secret-for-{iid}", "clientSecretData": {"id": "cs1"}}

        if method == "POST" and path.startswith("/v1/auth/universal-auth/identities/"):
            iid = path.rstrip("/").split("/")[-1]
            return {"identityUniversalAuth": {"id": "ua1", "clientId": f"client-for-{iid}"}}

        if method == "GET" and path.startswith("/v1/auth/universal-auth/identities/"):
            iid = path.rstrip("/").split("/")[-1]
            return {"identityUniversalAuth": {"id": "ua1", "clientId": f"client-for-{iid}"}}

        if method == "POST" and path == "/v1/auth/universal-auth/login":
            self.logins.append(body)
            client_id = body["clientId"]
            client_secret = body["clientSecret"]
            if self.valid_admin_client_secret is not None and client_secret != self.valid_admin_client_secret:
                raise si.ApiError("POST /v1/auth/universal-auth/login: HTTP 401 invalid client secret", 401)
            return {"accessToken": f"login-token-for-{client_id}", "tokenType": "Bearer"}

        if method == "GET" and path.startswith("/v2/workspace/") and "/identity-memberships/" in path:
            parts = path.split("/")
            pid, iid = parts[3], parts[5]
            if (pid, iid) not in self.memberships:
                raise si.ApiError(f"GET {path}: HTTP 404 not found", 404)
            return {}

        if method == "POST" and path.startswith("/v2/workspace/") and "/identity-memberships/" in path:
            parts = path.split("/")
            pid, iid = parts[3], parts[5]
            self.memberships.add((pid, iid))
            return {}

        if method == "GET" and path.startswith("/v3/secrets/raw/"):
            name_and_query = path[len("/v3/secrets/raw/"):]
            name, _, qs = name_and_query.partition("?")
            q = urllib.parse.parse_qs(qs)
            pid = q["workspaceId"][0]
            key = (pid, name)
            if key not in self.secrets:
                raise si.ApiError(f"GET {path}: HTTP 404 not found", 404)
            return {"secret": {"secretValue": self.secrets[key]}}

        if method in ("POST", "PATCH") and path.startswith("/v3/secrets/raw/"):
            name = path[len("/v3/secrets/raw/"):]
            pid = body["workspaceId"]
            self.secrets[(pid, name)] = body["secretValue"]
            return {}

        raise AssertionError(f"unhandled fake call: {method} {path}")


class FakeCli:
    def __init__(self, already_bootstrapped=False, verify_value=None, saved_token=None):
        self.calls = []
        self.already_bootstrapped = already_bootstrapped
        self.verify_value = verify_value
        self.saved_token = saved_token

    def __call__(self, argv, env_extra=None):
        self.calls.append(argv)
        if argv[0] == "infisical" and argv[1] == "bootstrap":
            if self.already_bootstrapped and self.saved_token is None:
                # simulate the CLI reporting "already bootstrapped" with no
                # fresh token in its JSON output.
                return json.dumps({"identity": {"credentials": {}}, "organization": {}})
            return json.dumps({
                "identity": {"id": "admin-identity-1", "credentials": {"token": "instance-admin-token-1"}},
                "organization": {"id": "org-1"},
            })
        if os.path.basename(argv[0]) == "capsule-secret":
            if self.verify_value is None:
                raise RuntimeError("capsule-secret: no identity file")
            return self.verify_value + "\n"
        raise AssertionError(f"unhandled fake cli call: {argv}")


def _write_sources(tmp):
    obs = Path(tmp) / "observability.env"
    obs.write_text(
        "OPS_PG_PASSWORD=pgpw123\n"
        "OPS_GRAFANA_PASSWORD=grafpw456\n"
        "OPS_TG_BOT_TOKEN=obstoken\n"
        "OPS_TG_CHAT_ID=555\n"
        "OPS_TG_THREAD_ID=7\n",
        encoding="utf-8",
    )
    ops = Path(tmp) / "ops.env"
    ops.write_text("OPS_TARGET_A=http://a\nOPS_TARGET_B=http://b\n", encoding="utf-8")
    return {"observability": str(obs), "ops": str(ops)}


class ReadSources(unittest.TestCase):
    @retired_only
    def test_no_legacy_telegram_source(self):
        cfg = Path(tempfile.mkdtemp())
        (cfg / "telegram.env").write_text("OPS_TG_BOT_TOKEN=leak\n")
        with mock.patch.dict(os.environ, {"CAPSULE_JOBS_DIR": str(cfg)}):
            self.assertNotIn("OPS_TG_BOT_TOKEN", si.read_sources())

    def test_default_sources_follow_the_jobs_config_dir(self):
        cfg = Path(tempfile.mkdtemp())
        (cfg / "observability.env").write_text("OPS_PG_PASSWORD=pw\n")
        (cfg / "ops.env").write_text("OPS_TARGET_A=http://a\n")
        with mock.patch.dict(os.environ, {"CAPSULE_JOBS_DIR": str(cfg)}):
            values = si.read_sources()
        self.assertEqual((values["OPS_PG_PASSWORD"], values["OPS_TARGET_A"]), ("pw", "http://a"))

    def test_parses_quoted_values_and_canary(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _write_sources(tmp)
            values = si.read_sources(paths)
        self.assertEqual(values["OPS_PG_PASSWORD"], "pgpw123")
        self.assertEqual(values["OPS_TARGET_A"], "http://a")
        self.assertEqual(values["CAPSULE_CHECK"], "ok")

    def test_missing_files_still_yield_canary(self):
        values = si.read_sources({
            "observability": "/no/such/file2",
            "ops": "/no/such/file3",
        })
        self.assertEqual(values, {"CAPSULE_CHECK": "ok"})


class RunFirstAndSecond(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_dir = Path(self.tmp.name) / "config"
        self.source_paths = _write_sources(self.tmp.name)

    def test_first_run_creates_everything_and_verifies(self):
        api = FakeApi()
        cli = FakeCli(verify_value=VERIFY_SECRET)
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = si.run(api=api, cli=cli, config_dir=self.config_dir, sources=self.source_paths)

        self.assertTrue(result["bootstrapped"])
        self.assertEqual(set(result["projects"]), set(si.project_slugs()))
        self.assertEqual(set(result["identities"]), set(si.identity_projects()))
        self.assertIn("OPS_PG_PASSWORD", result["imported"])
        self.assertTrue(result["verified"])

        self.assertEqual(len(api.projects), 4)
        self.assertEqual(len(api.identities), 5)
        # bridge needs all 4 projects; the other 4 identities need 1 each.
        self.assertEqual(len(api.memberships), 4 + 4)

        # config files exist, are 0600, dirs are 0700.
        proj_env = self.config_dir / "projects.env"
        admin_env = self.config_dir / "admin.env"
        self.assertTrue(proj_env.exists())
        self.assertTrue(admin_env.exists())
        self.assertEqual(stat.S_IMODE(proj_env.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(admin_env.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.config_dir.stat().st_mode), 0o700)

        # The admin identity gets universal-auth attached and a client
        # secret minted on the first run, so re-runs never depend on the
        # expiring INSTANCE_ADMIN_TOKEN again.
        admin = si.read_admin_env(self.config_dir)
        self.assertEqual(admin["INFISICAL_ADMIN_CLIENT_ID"], "client-for-admin-identity-1")
        self.assertTrue(admin["INFISICAL_ADMIN_CLIENT_SECRET"])
        self.assertIn(
            ("POST", "/v1/auth/universal-auth/identities/admin-identity-1"), api.calls
        )
        self.assertIn(
            ("POST", "/v1/auth/universal-auth/identities/admin-identity-1/client-secrets"),
            api.calls,
        )

        ident_dir = self.config_dir / "identities"
        self.assertEqual(stat.S_IMODE(ident_dir.stat().st_mode), 0o700)
        for name in si.identity_projects():
            p = ident_dir / f"{name}.env"
            self.assertTrue(p.exists(), p)
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
            content = p.read_text()
            self.assertIn("INFISICAL_CLIENT_ID=", content)
            self.assertIn("INFISICAL_CLIENT_SECRET=", content)

        # values never appear in stdout
        printed = buf.getvalue()
        self.assertNotIn(VERIFY_SECRET, printed)
        self.assertNotIn("pgpw123", printed)
        self.assertIn("secrets: imported", printed)
        self.assertIn("verified yes", printed)

    def test_second_run_is_a_pure_get_and_imports_nothing(self):
        api = FakeApi()
        cli1 = FakeCli(verify_value=VERIFY_SECRET)
        si.run(api=api, cli=cli1, config_dir=self.config_dir, sources=self.source_paths)

        api.calls.clear()
        cli2 = FakeCli(verify_value=VERIFY_SECRET, saved_token="reuse")
        result = si.run(api=api, cli=cli2, config_dir=self.config_dir, sources=self.source_paths)

        self.assertFalse(result["bootstrapped"])
        self.assertEqual(result["imported"], [])
        self.assertTrue(result["verified"])
        # Every call after the admin's universal-auth login must be a GET;
        # the login itself is the one POST a credentialed rerun makes.
        self.assertEqual(
            [c for c in api.calls if c[0] == "POST"],
            [("POST", "/v1/auth/universal-auth/login")],
            api.calls,
        )
        methods = {m for m, _ in api.calls}
        self.assertEqual(methods, {"GET", "POST"}, api.calls)

    def test_bootstrap_already_done_reads_saved_admin_token(self):
        api = FakeApi()
        cli1 = FakeCli(verify_value=VERIFY_SECRET)
        si.run(api=api, cli=cli1, config_dir=self.config_dir, sources=self.source_paths)
        saved_token = si.read_admin_env(self.config_dir)["INSTANCE_ADMIN_TOKEN"]
        self.assertEqual(saved_token, "instance-admin-token-1")

        # Simulate a bootstrap CLI call that reports "already bootstrapped"
        # with no fresh token in its JSON - the script must fall back to
        # admin.env's saved token rather than failing.
        api2 = FakeApi()
        api2.projects = dict(api.projects)
        api2.project_envs = dict(api.project_envs)
        api2.identities = dict(api.identities)
        api2.memberships = set(api.memberships)
        api2.secrets = dict(api.secrets)
        cli2 = FakeCli(already_bootstrapped=True, verify_value=VERIFY_SECRET)
        result = si.run(api=api2, cli=cli2, config_dir=self.config_dir, sources=self.source_paths)
        self.assertEqual(result["imported"], [])


class AdminUniversalAuthLogin(unittest.TestCase):
    """The instance-admin token `infisical bootstrap` prints has a max age
    and expires (live finding: ~2.5h later a re-run got a 401). Re-runs must
    authenticate forever via universal-auth client credentials instead."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_dir = Path(self.tmp.name) / "config"
        self.source_paths = _write_sources(self.tmp.name)

    def test_second_run_authenticates_via_universal_auth_login(self):
        api = FakeApi()
        cli1 = FakeCli(verify_value=VERIFY_SECRET)
        si.run(api=api, cli=cli1, config_dir=self.config_dir, sources=self.source_paths)
        client_id = si.read_admin_env(self.config_dir)["INFISICAL_ADMIN_CLIENT_ID"]

        api.calls.clear()
        api.logins.clear()
        cli2 = FakeCli(verify_value=VERIFY_SECRET, saved_token="reuse")
        si.run(api=api, cli=cli2, config_dir=self.config_dir, sources=self.source_paths)

        self.assertEqual(len(api.logins), 1, api.logins)
        self.assertEqual(api.logins[0]["clientId"], client_id)
        # A credentialed rerun is GETs plus exactly the one login POST.
        self.assertEqual(
            [c for c in api.calls if c[0] == "POST"],
            [("POST", "/v1/auth/universal-auth/login")],
            api.calls,
        )

    def test_admin_client_secret_never_printed(self):
        api = FakeApi()
        cli = FakeCli(verify_value=VERIFY_SECRET)
        buf = io.StringIO()
        with redirect_stdout(buf):
            si.run(api=api, cli=cli, config_dir=self.config_dir, sources=self.source_paths)
        admin = si.read_admin_env(self.config_dir)
        client_secret = admin["INFISICAL_ADMIN_CLIENT_SECRET"]
        self.assertTrue(client_secret)
        self.assertNotIn(client_secret, buf.getvalue())


class WaitForStatus(unittest.TestCase):
    def test_retries_then_succeeds(self):
        api = FakeApi()
        api.script_failure("GET", "/status", exc=RuntimeError("GET /status: connection refused"), times=3)
        sleeps = []
        si.wait_for_status(api, "http://localhost:8085/api", attempts=30, delay=2.0, sleep=sleeps.append)
        # 3 failures then a success: 3 sleeps between attempts, 4 calls total.
        self.assertEqual(sleeps, [2.0, 2.0, 2.0])
        self.assertEqual(api.calls, [("GET", "/status")] * 4)

    def test_raises_after_exhausting_attempts_naming_status(self):
        api = FakeApi()
        api.status_ok = False
        with self.assertRaises(RuntimeError) as ctx:
            si.wait_for_status(api, "http://localhost:8085/api", attempts=5, delay=0, sleep=lambda s: None)
        self.assertIn("/status", str(ctx.exception))
        self.assertEqual(len(api.calls), 5)

    def test_run_waits_for_status_first(self):
        api = FakeApi()
        api.script_failure("GET", "/status", times=2)
        cli = FakeCli(verify_value=VERIFY_SECRET)
        with tempfile.TemporaryDirectory() as tmp:
            paths = _write_sources(tmp)
            config_dir = Path(tmp) / "config"
            buf = io.StringIO()
            with redirect_stdout(buf):
                si.run(api=api, cli=cli, config_dir=config_dir, sources=paths,
                       sleep=lambda s: None)
        self.assertEqual(api.calls[0], ("GET", "/status"))


class RawSecretsErrorText(unittest.TestCase):
    """Item 2: HTTP error bodies must never leak for /v3/secrets/raw/ paths."""

    SENTINEL = "sekrit-body-value-should-never-leak"

    def _fake_http_error(self, path):
        import io as _io
        body = self.SENTINEL.encode("utf-8")

        err = urllib.error.HTTPError(
            url=f"http://localhost:8085/api{path}", code=400, msg="Bad Request",
            hdrs=None, fp=_io.BytesIO(body),
        )
        self.addCleanup(err.close)
        return err

    def test_raw_secrets_path_excludes_body(self):
        import unittest.mock as mock
        err = self._fake_http_error("/v3/secrets/raw/OPS_PG_PASSWORD")
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as ctx:
                si._api("POST", "/v3/secrets/raw/OPS_PG_PASSWORD",
                        body={"secretValue": "x"}, token="tok", base="http://localhost:8085/api")
        self.assertNotIn(self.SENTINEL, str(ctx.exception))
        self.assertIn("/v3/secrets/raw/OPS_PG_PASSWORD", str(ctx.exception))

    def test_raw_secrets_read_path_also_excludes_body(self):
        import unittest.mock as mock
        err = self._fake_http_error("/v3/secrets/raw/OPS_PG_PASSWORD")
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as ctx:
                si._api("GET", "/v3/secrets/raw/OPS_PG_PASSWORD?workspaceId=p&environment=prod",
                        token="tok", base="http://localhost:8085/api")
        self.assertNotIn(self.SENTINEL, str(ctx.exception))

    def test_other_paths_still_include_truncated_body(self):
        import unittest.mock as mock
        err = self._fake_http_error("/v2/workspace")
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as ctx:
                si._api("POST", "/v2/workspace", body={"slug": "x"}, token="tok",
                        base="http://localhost:8085/api")
        self.assertIn(self.SENTINEL, str(ctx.exception))

    def test_main_never_prints_the_sentinel(self):
        err = self._fake_http_error("/v3/secrets/raw/OPS_PG_PASSWORD")

        def boom(**kw):
            import unittest.mock as mock
            with mock.patch("urllib.request.urlopen", side_effect=err):
                si._api("POST", "/v3/secrets/raw/OPS_PG_PASSWORD",
                        body={"secretValue": "x"}, token="tok", base="http://localhost:8085/api")

        old_run = si.run
        try:
            si.run = boom
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = si.main([])
            self.assertEqual(code, 2)
            self.assertNotIn(self.SENTINEL, buf.getvalue())
            self.assertIn("/v3/secrets/raw/OPS_PG_PASSWORD", buf.getvalue())
        finally:
            si.run = old_run


class EnsureProjectTryScope(unittest.TestCase):
    """Item 3: the try/except around ensure_project must wrap only the GET
    workspace lookup; the environment-creation POST runs outside it."""

    def test_existing_project_env_post_failure_raises_without_creating_workspace(self):
        api = FakeApi()
        api.projects["capsule-corp"] = "proj-existing"
        api.project_envs["proj-existing"] = set()  # no "prod" env yet
        api.script_failure("POST", "/v1/workspace/proj-existing/environments",
                            exc=RuntimeError("POST /v1/workspace/proj-existing/environments: HTTP 500 boom"))

        with self.assertRaises(RuntimeError):
            si.ensure_project(api, "tok", "http://localhost:8085/api", "capsule-corp")

        self.assertNotIn(("POST", "/v2/workspace"), api.calls)
        self.assertIn(("POST", "/v1/workspace/proj-existing/environments"), api.calls)

    def test_non_404_get_failure_is_reraised_not_swallowed(self):
        api = FakeApi()
        api.script_failure("GET", "/v2/workspace/capsule-corp",
                            exc=RuntimeError("GET /v2/workspace/capsule-corp: HTTP 500 boom"))

        with self.assertRaises(RuntimeError) as ctx:
            si.ensure_project(api, "tok", "http://localhost:8085/api", "capsule-corp")
        self.assertIn("HTTP 500", str(ctx.exception))
        self.assertNotIn(("POST", "/v2/workspace"), api.calls)

    def test_missing_project_404_still_creates_it(self):
        api = FakeApi()
        project_id = si.ensure_project(api, "tok", "http://localhost:8085/api", "capsule-corp")
        self.assertEqual(project_id, "proj-capsule-corp")
        self.assertIn(("POST", "/v2/workspace"), api.calls)
        self.assertIn(("POST", "/v1/workspace/proj-capsule-corp/environments"), api.calls)


class MainExitCode(unittest.TestCase):
    def test_exit_2_on_failure_names_the_endpoint(self):
        def boom(argv):
            raise RuntimeError("infisical: exit 1: connection refused")

        old_run = si.run
        try:
            si.run = lambda **kw: (_ for _ in ()).throw(RuntimeError("POST /v2/workspace: HTTP 500 boom"))
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = si.main([])
            self.assertEqual(code, 2)
            self.assertIn("/v2/workspace", buf.getvalue())
        finally:
            si.run = old_run

    def test_exit_1_when_verified_false(self):
        """Important 5: main() must not report success (exit 0) when run()
        completed but its own verify step failed - that is precisely the
        scenario (capsule-secret not on PATH, wrong role, deleted identity
        file) an unattended overnight run must not silently paper over."""
        old_run = si.run
        try:
            si.run = lambda **kw: {
                "bootstrapped": True, "projects": {}, "identities": [],
                "imported": [], "verified": False,
            }
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = si.main([])
            self.assertEqual(code, 1)
        finally:
            si.run = old_run

    def test_exit_0_when_verified_true(self):
        old_run = si.run
        try:
            si.run = lambda **kw: {
                "bootstrapped": False, "projects": {}, "identities": [],
                "imported": [], "verified": True,
            }
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = si.main([])
            self.assertEqual(code, 0)
        finally:
            si.run = old_run


class MembershipAndSecretFailuresNotSwallowed(unittest.TestCase):
    """Important 3: ensure_membership and get_secret (via put_secret) must
    re-raise a non-404 ApiError instead of reading it as "doesn't exist yet"
    - the exact defect already fixed once in ensure_project."""

    def test_membership_non_404_get_failure_is_reraised_not_swallowed(self):
        api = FakeApi()
        api.script_failure(
            "GET", "/v2/workspace/proj-1/identity-memberships/id-1",
            exc=si.ApiError("GET .../identity-memberships/id-1: HTTP 500 boom", 500),
        )
        with self.assertRaises(si.ApiError) as ctx:
            si.ensure_membership(api, "tok", "http://localhost:8085/api", "proj-1", "id-1")
        self.assertEqual(ctx.exception.status, 500)
        self.assertNotIn(("POST", "/v2/workspace/proj-1/identity-memberships/id-1"), api.calls)

    def test_membership_404_still_creates_it(self):
        api = FakeApi()
        si.ensure_membership(api, "tok", "http://localhost:8085/api", "proj-1", "id-1")
        self.assertIn(("POST", "/v2/workspace/proj-1/identity-memberships/id-1"), api.calls)

    def test_get_secret_non_404_failure_is_reraised_not_swallowed(self):
        api = FakeApi()
        api.script_failure(
            "GET", "/v3/secrets/raw/X?workspaceId=proj-1&environment=prod&secretPath=%2F&viewSecretValue=true",
            exc=si.ApiError("GET .../secrets/raw/X: HTTP 500 boom", 500),
        )
        with self.assertRaises(si.ApiError):
            si.get_secret(api, "tok", "http://localhost:8085/api", "proj-1", "X")

    def test_put_secret_does_not_spuriously_create_on_transient_read_failure(self):
        """A 500 on the read-before-write must abort the write, not be
        treated as "secret does not exist" and issue a create POST."""
        api = FakeApi()
        api.script_failure(
            "GET", "/v3/secrets/raw/X?workspaceId=proj-1&environment=prod&secretPath=%2F&viewSecretValue=true",
            exc=si.ApiError("GET .../secrets/raw/X: HTTP 500 boom", 500),
        )
        with self.assertRaises(si.ApiError):
            si.put_secret(api, "tok", "http://localhost:8085/api", "proj-1", "X", "value")
        self.assertNotIn(("POST", "/v3/secrets/raw/X"), api.calls)

    def test_get_secret_404_returns_none(self):
        api = FakeApi()
        self.assertIsNone(si.get_secret(api, "tok", "http://localhost:8085/api", "proj-1", "X"))


class WaitForStatusSurvivesTimeout(unittest.TestCase):
    """Important 4: a socket timeout during the read phase surfaces as
    TimeoutError (an OSError), not a RuntimeError - wait_for_status must
    retry through it rather than crash on the failure it exists to survive."""

    def test_timeout_error_is_retried_then_succeeds(self):
        api = FakeApi()
        api.script_failure("GET", "/status", exc=TimeoutError("timed out"), times=2)
        sleeps = []
        si.wait_for_status(api, "http://localhost:8085/api", attempts=5, delay=1.0, sleep=sleeps.append)
        self.assertEqual(sleeps, [1.0, 1.0])
        self.assertEqual(api.calls, [("GET", "/status")] * 3)


class ApiConvertsOsErrorToRuntimeError(unittest.TestCase):
    """Important 4: _api must convert a read-phase TimeoutError/OSError
    (urlopen succeeds, but reading the response never completes) into a
    RuntimeError, the same shape wait_for_status already retries on."""

    def test_timeout_during_read_becomes_runtime_error(self):
        import unittest.mock as mock
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(RuntimeError) as ctx:
                si._api("GET", "/status", base="http://localhost:8085/api")
        self.assertNotIsInstance(ctx.exception, si.ApiError)
        self.assertIn("/status", str(ctx.exception))


class IdentityFileRepairedOnRerun(unittest.TestCase):
    """Important 6: an identity that already exists but whose env file is
    missing (interrupted first run, or the file was deleted by hand) must
    be repaired on the next run, not silently left unusable forever - and
    left alone (no mint) once the file exists, so the "second run is a pure
    GET" property still holds."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_dir = Path(self.tmp.name) / "config"
        self.source_paths = _write_sources(self.tmp.name)

    def test_missing_identity_file_is_repaired_with_a_fresh_client_secret(self):
        api = FakeApi()
        cli1 = FakeCli(verify_value=VERIFY_SECRET)
        si.run(api=api, cli=cli1, config_dir=self.config_dir, sources=self.source_paths)

        bridge_file = self.config_dir / "identities" / "bridge.env"
        self.assertTrue(bridge_file.exists())
        bridge_file.unlink()

        api.calls.clear()
        cli2 = FakeCli(verify_value=VERIFY_SECRET, saved_token="reuse")
        result = si.run(api=api, cli=cli2, config_dir=self.config_dir, sources=self.source_paths)

        self.assertTrue(bridge_file.exists(), "bridge.env must be rewritten on the rerun")
        content = bridge_file.read_text()
        self.assertIn("INFISICAL_CLIENT_ID=", content)
        self.assertIn("INFISICAL_CLIENT_SECRET=", content)
        self.assertIn(("POST", "/v1/auth/universal-auth/identities/id-bridge/client-secrets"), api.calls)
        self.assertTrue(result["verified"])

    def test_present_identity_file_is_left_alone_second_run_still_pure_get(self):
        api = FakeApi()
        cli1 = FakeCli(verify_value=VERIFY_SECRET)
        si.run(api=api, cli=cli1, config_dir=self.config_dir, sources=self.source_paths)

        api.calls.clear()
        cli2 = FakeCli(verify_value=VERIFY_SECRET, saved_token="reuse")
        si.run(api=api, cli=cli2, config_dir=self.config_dir, sources=self.source_paths)
        # Every call after the admin's universal-auth login must be a GET;
        # the login itself is the one POST a credentialed rerun makes.
        self.assertEqual(
            [c for c in api.calls if c[0] == "POST"],
            [("POST", "/v1/auth/universal-auth/login")],
            api.calls,
        )
        methods = {m for m, _ in api.calls}
        self.assertEqual(methods, {"GET", "POST"}, api.calls)


class WriteSecureChmodsConfigDirRegardlessOfOrder(unittest.TestCase):
    """Minor 3: config_dir itself must end up 0700 even when the only file
    written under it is nested (identities/<name>.env), not just when
    projects.env (a direct child) happens to be written first."""

    def test_config_dir_is_0700_after_only_a_nested_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "capsule"
            si.write_identity_env(config_dir, "bridge", "cid", "csecret")
            self.assertEqual(stat.S_IMODE(config_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((config_dir / "identities").stat().st_mode), 0o700)


class BootstrapPasswordOffArgv(unittest.TestCase):
    """Intent example bootstrap-password-off-argv: a mock `infisical` on PATH records its
    argv and environment; the admin password reaches it by env, never on argv."""

    def test_mock_infisical_sees_password_in_env_not_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            bindir, rec = Path(tmp) / "bin", Path(tmp) / "rec"
            bindir.mkdir()
            mock_cli = bindir / "infisical"
            mock_cli.write_text(
                '#!/bin/sh\n'
                f'printf "%s\\n" "$@" >"{rec}.argv"\n'
                f'printf "%s" "$INFISICAL_ADMIN_PASSWORD" >"{rec}.pw"\n'
                'echo \'{"identity": {"id": "i", "credentials": {"token": "t"}}, '
                '"organization": {"id": "o"}}\'\n')
            mock_cli.chmod(mock_cli.stat().st_mode | stat.S_IXUSR)
            config_dir = Path(tmp) / "config"
            config_dir.mkdir()
            si._write_secure(config_dir / "admin.env",
                             "INFISICAL_ADMIN_EMAIL=a@example.com\n"
                             "INFISICAL_ADMIN_PASSWORD=pw-sentinel-123\n", config_dir=config_dir)
            with mock.patch.dict(os.environ, {"PATH": f"{bindir}:{os.environ['PATH']}"}):
                si.ensure_bootstrap(si._default_cli, config_dir, "http://x", api=FakeApi())
            argv = Path(f"{rec}.argv").read_text()
            self.assertIn("bootstrap", argv)
            self.assertNotIn("pw-sentinel-123", argv)
            self.assertNotIn("--password", argv)
            self.assertEqual(Path(f"{rec}.pw").read_text(), "pw-sentinel-123")


class DefaultCliErrorText(unittest.TestCase):
    """Important 7: the exception _default_cli raises on a nonzero exit
    must never carry the CLI's stderr - the one command run through it with
    a secret in its environment is `infisical bootstrap` (INFISICAL_ADMIN_PASSWORD),
    and a CLI that echoes a validation message on a bad password would put
    that password into main()'s printed output."""

    def test_error_message_excludes_stderr(self):
        import unittest.mock as mock
        result = mock.Mock(returncode=1, stdout="", stderr="leaked-admin-password-xyz")
        with mock.patch("subprocess.run", return_value=result) as run:
            with self.assertRaises(RuntimeError) as ctx:
                si._default_cli(["infisical", "bootstrap"],
                                env_extra={"INFISICAL_ADMIN_PASSWORD": "leaked-admin-password-xyz"})
        self.assertEqual(str(ctx.exception), "infisical bootstrap: exit 1")
        self.assertNotIn("leaked-admin-password-xyz", str(ctx.exception))

    def test_capsule_secret_call_gets_verify_identity_in_env(self):
        import unittest.mock as mock
        result = mock.Mock(returncode=0, stdout="value\n", stderr="")
        with mock.patch("subprocess.run", return_value=result) as run:
            si._default_cli([si.CAPSULE_SECRET_BIN, "get", "capsule-corp", "OPS_PG_PASSWORD"])
        _, kwargs = run.call_args
        self.assertEqual(kwargs["env"]["CAPSULE_IDENTITY"], si.VERIFY_IDENTITY)

    def test_infisical_call_does_not_force_capsule_identity(self):
        import unittest.mock as mock
        result = mock.Mock(returncode=0, stdout="{}", stderr="")
        with mock.patch("subprocess.run", return_value=result) as run:
            si._default_cli(["infisical", "bootstrap"])
        _, kwargs = run.call_args
        self.assertIsNone(kwargs["env"])




class ApiErrorShapes(unittest.TestCase):
    def test_bad_json_response_is_a_runtime_error_without_body(self):
        import io, urllib.request
        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        with unittest.mock.patch.object(urllib.request, "urlopen", return_value=Resp(b"SENTINEL-not-json")):
            with self.assertRaises(RuntimeError) as cm:
                si._api("GET", "/v3/secrets/raw/X", base="http://x")
        self.assertNotIn("SENTINEL", str(cm.exception))

    def test_http_error_carries_numeric_status(self):
        import io, urllib.request, urllib.error
        err = urllib.error.HTTPError("http://x/v2/workspace/p", 500, "boom", {}, io.BytesIO(b"upstream said HTTP 404"))
        with unittest.mock.patch.object(urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(si.ApiError) as cm:
                si._api("GET", "/v2/workspace/p", base="http://x")
        self.assertEqual(cm.exception.status, 500)


def setUpModule():
    """Never read the machine's real host.json: this suite's own host config."""
    home = tempfile.mkdtemp()
    Path(home, "host.json").write_text(
        '{"secrets": {"projects": ["capsule-corp", "agent-ops", "sample-a", "sample-b"]}}')
    global _PATCH
    _PATCH = mock.patch.dict(os.environ, {"CAPSULE_HOME": home})
    _PATCH.start()


def tearDownModule():
    _PATCH.stop()


if __name__ == "__main__":
    unittest.main()
