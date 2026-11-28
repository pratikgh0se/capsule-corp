"""Fix round 2 item 2: irreversible overrides need an interactive terminal to even write the
human file - a subprocess check, since `sys.stdin.isatty()`/`sys.stdout.isatty()` only mean
anything through a real process boundary."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from capsule.tests.private_names import personal_names

ROUTING = {r: {"provider": "p", "account": "a", "model": "m", "turn_budget": 1}
          for r in ("implementer", "reviewer", "validator", "guide")}


def _cfg_home(contract_commands, operator="owner"):
    home = Path(tempfile.mkdtemp())
    cfg = {"board": "test", "epics": ["e"],
           "caps": {"max_running": 1, "claim_ttl": 900, "failure_limit": 3, "checkpoint_chain": 3,
                    "tick_seconds": 30},
           "contract_commands": contract_commands, "routing": ROUTING, "operator": operator}
    (home / "config.json").write_text(json.dumps(cfg))
    return home


def _identity_dir():
    d = Path(tempfile.mkdtemp())
    f = d / "owner.env"
    f.write_text("x\n")
    os.chmod(f, 0o600)
    return d


def _run(cfg_home, check, **extra_env):
    env = dict(os.environ, CAPSULE_IDENTITY="owner", CAPSULE_IDENTITY_DIR=str(_identity_dir()),
               **extra_env)
    return subprocess.run(
        [sys.executable, "-m", "capsule", "human", "test", "t_x", "override", "reason",
         "--check", check, "--ttl", "72h", "--fix-card", "t_fix1", "--author", "cos",
         "--capsule-home", str(cfg_home)],
        env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)   # /dev/null: never a TTY


class IrreversibleTty(unittest.TestCase):
    def test_irreversible_check_refused_without_a_tty(self):
        r = _run(_cfg_home(["merge"]), "merge")
        self.assertEqual(r.returncode, 1)
        self.assertIn("interactive terminal", r.stderr)

    def test_irreversible_check_by_index_also_refused_without_a_tty(self):
        r = _run(_cfg_home(["merge"]), "0")
        self.assertEqual(r.returncode, 1)
        self.assertIn("interactive terminal", r.stderr)

    def test_non_irreversible_check_needs_no_tty(self):
        r = _run(_cfg_home(["lint"]), "lint")
        self.assertEqual(r.returncode, 0)
        self.assertTrue(Path(r.stdout.strip()).exists())

    def test_unloadable_config_fails_closed_even_for_a_check_text_absent_from_the_default_list(self):
        """Round 3, re-review 2 m2: no config.json at all must not let a check id through the
        gate just because it happens not to spell 'merge'/'deploy'/'give-back'."""
        home = Path(tempfile.mkdtemp())   # no config.json written
        r = _run(home, "lint")
        self.assertEqual(r.returncode, 1)
        self.assertIn("interactive terminal", r.stderr)

    def test_error_text_does_not_overclaim_the_tty_gate(self):
        """Round 3, re-review 2 I-1: the gate raises the bar for host agents; it does not claim
        to stop a same-user process that allocates a pty."""
        r = _run(_cfg_home(["merge"]), "merge")
        self.assertNotIn("never a TTY", r.stderr)
        for name in personal_names():
            self.assertNotIn(f"anything but {name} typing", r.stderr.lower())
        self.assertIn("accepted residual", r.stderr)


class OverrideBoardConfig(unittest.TestCase):
    """`human <board> ... override` resolves that board's own config for its irreversible-checks
    list, not the legacy one."""

    def test_reads_the_named_boards_own_config_not_the_legacy_one(self):
        home = Path(tempfile.mkdtemp())
        (home / "config.json").write_text(json.dumps(
            {"board": "legacy", "epics": ["e"],
             "caps": {"max_running": 1, "claim_ttl": 900, "failure_limit": 3, "checkpoint_chain": 3,
                      "tick_seconds": 30},
             "contract_commands": ["safe_op"], "routing": ROUTING, "operator": "owner"}))
        d = home / "boards" / "other"
        d.mkdir(parents=True)
        (d / "config.json").write_text(json.dumps(
            {"board": "other", "epics": ["e"],
             "caps": {"max_running": 1, "claim_ttl": 900, "failure_limit": 3, "checkpoint_chain": 3,
                      "tick_seconds": 30},
             "contract_commands": ["safe_op"], "irreversible_checks": ["safe_op"],
             "routing": ROUTING, "operator": "owner"}))
        env = dict(os.environ, CAPSULE_IDENTITY="owner", CAPSULE_IDENTITY_DIR=str(_identity_dir()))
        r = subprocess.run(
            [sys.executable, "-m", "capsule", "human", "other", "t_x", "override", "reason",
             "--check", "safe_op", "--ttl", "72h", "--fix-card", "t_fix1", "--author", "cos",
             "--capsule-home", str(home)],
            env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)   # /dev/null: never a TTY
        # legacy's config would treat "safe_op" as reversible (not in the default irreversible
        # set) and let this through with no tty needed; "other"'s config marks it irreversible.
        self.assertEqual(r.returncode, 1)
        self.assertIn("interactive terminal", r.stderr)


if __name__ == "__main__":
    unittest.main()
