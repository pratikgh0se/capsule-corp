"""W362: the Infisical admin email is configuration, never a default shipped in the repo."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import secrets_import as si  # noqa: E402
from test_secrets_import import (  # noqa: E402
    FakeApi, FakeCli, VERIFY_SECRET, _write_sources,
)


class ResolveAdminEmail(unittest.TestCase):
    def test_env_var_wins_over_saved_admin_env(self):
        with mock.patch.dict(os.environ, {"CAPSULE_ADMIN_EMAIL": "env@example.com"}):
            got = si.resolve_admin_email({"INFISICAL_ADMIN_EMAIL": "saved@example.com"})
        self.assertEqual(got, "env@example.com")

    def test_saved_admin_env_is_used_when_no_env_var(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("CAPSULE_ADMIN_EMAIL", None)
            got = si.resolve_admin_email({"INFISICAL_ADMIN_EMAIL": "saved@example.com"})
        self.assertEqual(got, "saved@example.com")

    def test_git_config_is_the_last_fallback(self):
        done = subprocess.CompletedProcess([], 0, stdout="git@example.com\n")
        run = mock.patch.object(si.subprocess, "run", return_value=done)
        with mock.patch.dict(os.environ), run:
            os.environ.pop("CAPSULE_ADMIN_EMAIL", None)
            self.assertEqual(si.resolve_admin_email({}), "git@example.com")

    def test_missing_everywhere_is_a_clear_error(self):
        done = subprocess.CompletedProcess([], 1, stdout="")
        run = mock.patch.object(si.subprocess, "run", return_value=done)
        with mock.patch.dict(os.environ), run:
            os.environ.pop("CAPSULE_ADMIN_EMAIL", None)
            with self.assertRaises(RuntimeError) as cm:
                si.resolve_admin_email({})
        self.assertIn("CAPSULE_ADMIN_EMAIL", str(cm.exception))


if __name__ == "__main__":
    unittest.main()


def _bootstrap_email(cli):
    boot = next(c for c in cli.calls if c[:2] == ["infisical", "bootstrap"])
    return boot[boot.index("--email") + 1]


class LegacyAdminEnvUsesSavedEmail(unittest.TestCase):
    """Moved from test_secrets_import.py: a saved admin.env email feeds `infisical bootstrap`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_dir = Path(self.tmp.name) / "config"
        self.source_paths = _write_sources(self.tmp.name)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CAPSULE_ADMIN_EMAIL", None)

    def test_legacy_admin_env_without_client_creds_uses_instance_admin_token(self):
        """An admin.env saved before this fix has only INSTANCE_ADMIN_TOKEN
        (plus INFISICAL_ORG_ID) - no INFISICAL_ADMIN_CLIENT_ID/SECRET. As
        long as that token is still valid, the run must proceed using it,
        without attempting to attach universal-auth again (that only
        happens on a true first run, when admin.env does not exist yet)."""
        api = FakeApi()
        self.config_dir.mkdir(parents=True)
        si._write_secure(
            self.config_dir / "admin.env",
            "INFISICAL_ADMIN_EMAIL=saved-admin@example.com\n"
            "INFISICAL_ADMIN_PASSWORD=oldpw\n"
            "INSTANCE_ADMIN_TOKEN=legacy-token-1\n"
            "INFISICAL_ORG_ID=org-1\n",
            config_dir=self.config_dir,
        )
        cli = FakeCli(already_bootstrapped=True, verify_value=VERIFY_SECRET)
        result = si.run(api=api, cli=cli, config_dir=self.config_dir, sources=self.source_paths)
        self.assertEqual(_bootstrap_email(cli), "saved-admin@example.com")
        self.assertTrue(result["verified"])
        self.assertNotIn(
            ("POST", "/v1/auth/universal-auth/identities/admin-identity-1"), api.calls
        )
        admin = si.read_admin_env(self.config_dir)
        self.assertNotIn("INFISICAL_ADMIN_CLIENT_ID", admin)

    def test_legacy_admin_env_expired_token_fails_with_clear_recovery_message(self):
        api = FakeApi()
        api.script_failure(
            "GET", "/v2/organizations/org-1/identity-memberships",
            exc=si.ApiError(
                "GET /v2/organizations/org-1/identity-memberships: HTTP 401 "
                "Identity access token exceeded max age, please re-authenticate",
                401,
            ),
        )
        self.config_dir.mkdir(parents=True)
        si._write_secure(
            self.config_dir / "admin.env",
            "INFISICAL_ADMIN_EMAIL=saved-admin@example.com\n"
            "INFISICAL_ADMIN_PASSWORD=oldpw\n"
            "INSTANCE_ADMIN_TOKEN=legacy-token-1\n"
            "INFISICAL_ORG_ID=org-1\n",
            config_dir=self.config_dir,
        )
        cli = FakeCli(already_bootstrapped=True, verify_value=VERIFY_SECRET)
        with self.assertRaises(RuntimeError) as ctx:
            si.run(api=api, cli=cli, config_dir=self.config_dir, sources=self.source_paths)
        self.assertEqual(_bootstrap_email(cli), "saved-admin@example.com")
        msg = str(ctx.exception)
        self.assertIn("admin token expired", msg)
        self.assertIn("admin client credentials in admin.env", msg)
        self.assertIn("RUNBOOK", msg)
        self.assertNotIn("legacy-token-1", msg)
