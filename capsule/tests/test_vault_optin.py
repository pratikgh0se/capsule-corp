"""D254: the notes-vault features are opt-in. With no vault configured the vault jobs exit 0 with
one line and Korin's settings carry no vault; env or host.json turns them on. Temp dirs only."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maintenance"))
os.environ.setdefault("AGENT_OPS_NO_SEND", "1")

from capsule import incidents  # noqa: E402
import vault_jobs  # noqa: E402

VAULT_ENV, ROOT_ENV = "OBSIDIAN_VAULT_PATH", "OBSIDIAN_SKILL_ROOT"
_KEYS = ("CAPSULE_HOME", VAULT_ENV, ROOT_ENV)


class OptIn(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.saved = {k: os.environ.get(k) for k in _KEYS}
        os.environ["CAPSULE_HOME"] = str(self.home)
        os.environ.pop(VAULT_ENV, None)
        os.environ.pop(ROOT_ENV, None)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def cfg(self, vault_cfg):
        (self.home / "host.json").write_text(json.dumps({"vault": vault_cfg}))

    def run_job(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = vault_jobs.main(list(argv)) or 0
        return rc, out.getvalue().strip()

    def test_every_vault_job_is_a_clean_no_op_without_a_vault(self):
        for job in ("health", "morning", "digest"):
            self.assertEqual(self.run_job(job), (0, vault_jobs.SKIPPED), job)

    def test_vault_jobs_use_the_configured_vault(self):
        vault = Path(self.tmp.name) / "vault"
        (vault / "Daily").mkdir(parents=True)
        (vault / "Templates").mkdir()
        (vault / "Templates" / "Daily Note.md").write_text("# day\n")
        self.cfg({"path": str(vault)})
        self.assertEqual(vault_jobs.vault_path(), vault)
        rc, out = self.run_job("morning")
        self.assertEqual(rc, 0)
        self.assertNotEqual(out, vault_jobs.SKIPPED)

    def test_checker_root_comes_from_env_or_host_json_never_a_home_path(self):
        seen = []

        def runner(argv):
            seen.append(argv[argv.index("--directory") + 1])
            return "{}"
        with self.assertRaises(RuntimeError):
            vault_jobs.run_checker("/v")
        self.cfg({"skill_root": "/opt/checker"})
        vault_jobs.run_checker("/v", runner)
        os.environ[ROOT_ENV] = "/opt/other"
        vault_jobs.run_checker("/v", runner)
        self.assertEqual(seen, ["/opt/checker", "/opt/other"])

    def test_korin_settings_have_no_vault_by_default(self):
        self.assertIsNone(incidents.settings({})["vault"])

    def test_korin_settings_take_the_vault_from_host_json_env_or_block(self):
        self.cfg({"path": "/opt/v1"})
        self.assertEqual(incidents.settings({})["vault"], Path("/opt/v1"))
        os.environ[VAULT_ENV] = "/opt/v2"
        self.assertEqual(incidents.settings({})["vault"], Path("/opt/v2"))
        block = {"korin": {"vault": "/opt/v3"}}
        self.assertEqual(incidents.settings(block)["vault"], Path("/opt/v3"))

    def test_the_closure_gate_names_a_missing_vault_instead_of_raising(self):
        inc = {"status": "fix_pending", "id": "inc_x", "decision_row": 5, "note_path": "n.md"}
        lines = incidents.missing(None, inc, vault=None, check_new=None)
        self.assertTrue(any("post-mortem note missing" in m for m in lines), lines)


if __name__ == "__main__":
    unittest.main()
