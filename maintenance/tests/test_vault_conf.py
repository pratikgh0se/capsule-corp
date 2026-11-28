"""D254: the notes vault and its skill checkout are opt-in settings with no default directory.
vault_conf reads them (env, else host.json) and every vault feature skips with one clear line when
they are unset. All paths here are scratch dirs; CAPSULE_HOME points at a scratch host.json."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("AGENT_OPS_NO_SEND", "1")
import brief  # noqa: E402
import decisions  # noqa: E402
import merge_gate  # noqa: E402
import resume_pointers  # noqa: E402
import retrieval_eval_job  # noqa: E402
import vault_conf  # noqa: E402
try:
    import vault_stats  # noqa: E402
except ImportError:  # the public allowlist leaves it out
    vault_stats = None
no_stats = unittest.skipIf(vault_stats is None, "vault_stats is not in the public release")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vault-conf-"))
        self.home = self.tmp / "capsule-home"
        self.home.mkdir()
        env = {k: v for k, v in os.environ.items()
               if k not in ("OBSIDIAN_VAULT_PATH", "OBSIDIAN_SKILL_ROOT")}
        env["CAPSULE_HOME"] = str(self.home)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def host_json(self, **vault):
        (self.home / "host.json").write_text(json.dumps({"vault": vault}))


class Resolution(Base):
    def test_unset_everywhere_is_none(self):
        self.assertIsNone(vault_conf.vault())
        self.assertIsNone(vault_conf.skill_root())

    def test_host_json_is_read(self):
        self.host_json(path=str(self.tmp / "v"), skill_root=str(self.tmp / "s"))
        self.assertEqual(vault_conf.vault(), self.tmp / "v")
        self.assertEqual(vault_conf.skill_root(), self.tmp / "s")

    def test_env_wins_over_host_json(self):
        self.host_json(path=str(self.tmp / "v"), skill_root=str(self.tmp / "s"))
        os.environ["OBSIDIAN_VAULT_PATH"] = str(self.tmp / "env-v")
        os.environ["OBSIDIAN_SKILL_ROOT"] = str(self.tmp / "env-s")
        self.assertEqual(vault_conf.vault(), self.tmp / "env-v")
        self.assertEqual(vault_conf.skill_root(), self.tmp / "env-s")

    def test_host_json_vault_ignores_the_env_override(self):
        self.host_json(path=str(self.tmp / "v"))
        os.environ["OBSIDIAN_VAULT_PATH"] = str(self.tmp / "env-v")
        self.assertEqual(vault_conf.host_json_vault(), self.tmp / "v")


class SkipsCleanly(Base):
    """Nothing configured: each vault feature says so in one line and does not crash."""

    def run_quiet(self, fn, *args, **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = fn(*args, **kw)
        return result, out.getvalue() + err.getvalue()

    def test_retrieval_eval_job_skips_with_exit_zero(self):
        with mock.patch.object(retrieval_eval_job, "VAULT", None), \
                mock.patch.object(retrieval_eval_job, "SKILL_ROOT", None):
            rc, text = self.run_quiet(retrieval_eval_job.run)
        self.assertEqual(rc, 0)
        self.assertIn("no notes vault configured", text)

    def test_retrieval_eval_job_skips_when_only_the_skill_is_missing(self):
        with mock.patch.object(retrieval_eval_job, "VAULT", self.tmp), \
                mock.patch.object(retrieval_eval_job, "SKILL_ROOT", None):
            rc, text = self.run_quiet(retrieval_eval_job.run)
        self.assertEqual(rc, 0)
        self.assertIn("no notes-vault skill checkout configured", text)

    @no_stats
    def test_vault_stats_sample_skips(self):
        with mock.patch.object(vault_stats, "VAULT", None):
            result, text = self.run_quiet(vault_stats.sample, force=True)
        self.assertIsNone(result)
        self.assertIn("no notes vault configured", text)

    @no_stats
    def test_vault_stats_health_skips_the_real_checkers_without_a_skill_root(self):
        with mock.patch.object(vault_stats, "OSB_DIR", None):
            result, text = self.run_quiet(vault_stats.health, self.tmp)
        self.assertEqual(result, {})
        self.assertIn("no notes-vault skill checkout configured", text)

    def test_decisions_answer_refuses_without_a_vault(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            decisions.main(["answer", "my-app", "t_1", "yes"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("no notes vault configured", out.getvalue())

    def test_merge_gate_approve_spec_says_no_vault(self):
        rc, text = self.run_quiet(merge_gate.main, ["approve-spec", "some-slug"])
        self.assertEqual(rc, 1)
        self.assertIn("no notes vault configured", text)

    def test_resume_pointers_scan_says_no_vault(self):
        rc, text = self.run_quiet(resume_pointers._main, ["--scan"])
        self.assertEqual(rc, 0)
        self.assertIn("no notes vault configured", text)

    def test_brief_vault_copy_says_no_vault(self):
        os.environ.pop("AGENT_OPS_NO_SEND", None)   # the under-test refusal would answer first
        with mock.patch.object(brief, "VAULT", None):
            rel, text = self.run_quiet(brief.write_vault_copy, "text")
        self.assertIsNone(rel)
        self.assertIn("no notes vault configured", text)


if __name__ == "__main__":
    unittest.main()
