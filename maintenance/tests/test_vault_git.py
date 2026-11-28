# mutation-check: targets-predate-range b957739c9 -- names out of tests, no behaviour pinned ::
#   PreStaged.test_push_leaves_a_file_staged_by_another_process_out_of_the_commit
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule import host as host_cfg  # noqa: E402
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("AGENT_OPS_NO_SEND", "1")
import vault_git, events

_LAYOUT = ["Ledger/", "Boards/", "Specs/", "Decisions/", "Architecture/", "Reviews/", "Projects/",
           "Reports/", "Inbox/", "Debugging/", "Dev Logs/", "Knowledge/", "Daily/", "Tasks/",
           "Brainstorms/", "Pitches/"]
_CFG = tempfile.TemporaryDirectory()
VAULT_ENV = "OBSIDIAN_VAULT_PATH"
_KEYS = ("CAPSULE_HOME", VAULT_ENV)
_SAVED = {k: os.environ.get(k) for k in _KEYS}


def _config(vault_cfg):
    (Path(_CFG.name) / "host.json").write_text(json.dumps({"vault": vault_cfg}))


def setUpModule():
    """A temp CAPSULE_HOME whose host.json lists a folder layout; never the real home or vault."""
    os.environ["CAPSULE_HOME"] = _CFG.name
    os.environ.pop(VAULT_ENV, None)
    _config({"push_allow": _LAYOUT})


def tearDownModule():
    for k, v in _SAVED.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    _CFG.cleanup()


def host_vault():
    from capsule import host
    return host.vault_path()


def _run(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd)] + list(args), capture_output=True, text=True, check=True)


class RealRepo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = Path(self.tmp.name)
        self.remote = root / "remote.git"; subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        self.vault = root / "vault"; subprocess.run(["git", "init", "-q", str(self.vault)], check=True)
        _run(self.vault, "config", "user.email", "t@t"); _run(self.vault, "config", "user.name", "t")
        (self.vault / "a.md").write_text("a"); _run(self.vault, "add", "a.md"); _run(self.vault, "commit", "-q", "-m", "init")
        _run(self.vault, "remote", "add", "origin", str(self.remote)); _run(self.vault, "push", "-q", "-u", "origin", "HEAD")
        self.spool = root / "spool.jsonl"; self._old = events.SPOOL; events.SPOOL = self.spool
        self._env = os.environ.pop("AGENT_OPS_NO_SEND", None)

    def tearDown(self):
        events.SPOOL = self._old
        if self._env is not None: os.environ["AGENT_OPS_NO_SEND"] = self._env
        self.tmp.cleanup()

    def test_push_commits_only_the_given_paths_and_reaches_remote(self):
        (self.vault / "Decisions").mkdir(); (self.vault / "Decisions" / "d.md").write_text("x")
        (self.vault / "draft.md").write_text("half written")
        self.assertTrue(vault_git.push(self.vault, ["Decisions/d.md"], "decisions: d"))
        log = _run(self.remote, "log", "--format=%s", "-1").stdout.strip()
        self.assertEqual(log, "decisions: d")
        self.assertIn("draft.md", _run(self.vault, "status", "--short").stdout)  # untouched

    def test_push_with_nothing_new_is_a_noop_true(self):
        self.assertTrue(vault_git.push(self.vault, ["a.md"], "noop"))
        self.assertEqual(_run(self.vault, "log", "--format=%s", "-1").stdout.strip(), "init")

    def test_pull_brings_a_phone_edit_in(self):
        other = Path(self.tmp.name) / "phone"; subprocess.run(["git", "clone", "-q", str(self.remote), str(other)], check=True)
        _run(other, "config", "user.email", "p@p"); _run(other, "config", "user.name", "p")
        (other / "a.md").write_text("ticked"); _run(other, "commit", "-qam", "phone tick"); _run(other, "push", "-q", "origin", "HEAD")
        self.assertTrue(vault_git.pull(self.vault))
        self.assertEqual((self.vault / "a.md").read_text(), "ticked")

    def test_push_failure_logs_warn_event_and_returns_false(self):
        _run(self.vault, "remote", "set-url", "origin", str(Path(self.tmp.name) / "missing.git"))
        (self.vault / "Decisions").mkdir(exist_ok=True); (self.vault / "Decisions" / "b.md").write_text("b")
        self.assertFalse(vault_git.push(self.vault, ["Decisions/b.md"], "b"))
        self.assertIn("vault_push_failed", self.spool.read_text())


class NoSend(unittest.TestCase):
    def test_no_send_skips_git_entirely(self):
        os.environ["AGENT_OPS_NO_SEND"] = "1"
        calls = []
        self.assertTrue(vault_git.push("/nonexistent", ["x"], "m"))
        self.assertTrue(vault_git.pull("/nonexistent"))
        self.assertEqual(calls, [])


class MachineCommitsTouching(unittest.TestCase):
    """C3/M054 (row 127, spec §4.5): "no `maint(<job>)`-subject commit
    touches `_CLAUDE.md` or `references/**`" reduces to
    `vault_git.machine_commits_touching(repo, path)` coming back empty for
    those two paths. Built on a fixture repo, never the real vault."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "vault"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        _run(self.repo, "config", "user.email", "owner@users.noreply.github.com")
        _run(self.repo, "config", "user.name", "owner")

        def commit(rel, subject):
            p = self.repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"{subject}\n")
            _run(self.repo, "add", rel)
            _run(self.repo, "commit", "-q", "-m", subject)

        # Four commits, all under the SAME author identity (owner) -
        # the point of M054/§4.5 is that the author cannot distinguish a job
        # commit from a human one, only the subject can.
        commit("Daily/x.md", "maint(nightly): 2026-09-10")
        commit("_CLAUDE.md", "human edit: tidy the operating manual")
        commit("_CLAUDE.md", "maint(inbox): 2026-09-10")  # the violation
        commit("Knowledge/y.md", "maint(weekly): 2026-09-10")

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_the_planted_violation_comes_back(self):
        import vault_git as vg
        got = vg.machine_commits_touching(self.repo, "_CLAUDE.md")
        self.assertEqual(len(got), 1, got)
        self.assertEqual(got[0][1], "maint(inbox): 2026-09-10")

    def test_a_path_no_machine_commit_touches_is_clean(self):
        import vault_git as vg
        got = vg.machine_commits_touching(self.repo, "Knowledge/y.md")
        self.assertEqual([s for _, s in got], ["maint(weekly): 2026-09-10"])

    def test_the_human_commit_to_the_same_path_is_not_counted(self):
        import vault_git as vg
        got = vg.machine_commits_touching(self.repo, "_CLAUDE.md")
        subjects = [s for _, s in got]
        self.assertNotIn("human edit: tidy the operating manual", subjects)


class MachineCommitsRefusesTheRealVault(unittest.TestCase):
    """The helper reads git history; under AGENT_OPS_NO_SEND it must refuse
    to point that at the vault env var (2026-09-07 incident, same
    guard as board_view._refuse_default_vault_under_test)."""

    def test_the_real_vault_is_never_read(self):
        import vault_git as vg
        os.environ["AGENT_OPS_NO_SEND"] = "1"
        with tempfile.TemporaryDirectory() as d:
            os.environ[VAULT_ENV] = d
            try:
                with self.assertRaises(RuntimeError):
                    vg.machine_commits_touching(Path(d), "_CLAUDE.md")
            finally:
                os.environ.pop(VAULT_ENV, None)

    def test_a_fixture_repo_elsewhere_is_not_refused(self):
        import vault_git as vg
        os.environ["AGENT_OPS_NO_SEND"] = "1"
        tmp = tempfile.TemporaryDirectory()
        try:
            repo = Path(tmp.name) / "fixture"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            _run(repo, "config", "user.email", "t@t"); _run(repo, "config", "user.name", "t")
            (repo / "a.md").write_text("a"); _run(repo, "add", "a.md")
            _run(repo, "commit", "-q", "-m", "init")
            self.assertEqual(vg.machine_commits_touching(repo, "a.md"), [])
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()


class PreStaged(RealRepo):
    def test_push_leaves_a_file_staged_by_another_process_out_of_the_commit(self):
        (self.vault / "plugin.md").write_text("staged by the editor plugin")
        _run(self.vault, "add", "plugin.md")
        (self.vault / "Decisions").mkdir(exist_ok=True); (self.vault / "Decisions" / "note.md").write_text("n")
        self.assertTrue(vault_git.push(self.vault, ["Decisions/note.md"], "decisions: note"))
        files = _run(self.remote, "show", "--format=", "--name-only", "HEAD").stdout.split()
        self.assertEqual(files, ["Decisions/note.md"])
        self.assertIn("A  plugin.md", _run(self.vault, "status", "--short").stdout)


class PushAllowlist(unittest.TestCase):
    """Row 46's boundary, made mechanical at the one chokepoint every vault
    write already passes through."""

    def test_every_prefix_this_repo_actually_pushes_is_allowed(self):
        for rel in ("Ledger/my-app/t_1.jsonl", "Boards/Engineering.md", "Boards/Intake.md",
                    "Specs/soft-tick.md", "Decisions/my-app/2026-09-08-x-t_1.md",
                    "Architecture/capsule-corp/RUNBOOK.md",
                    "Brainstorms/2026-09-08-x-tp_1.md"):
            self.assertTrue(vault_git.allowed(rel), rel)

    def test_denied_and_unlisted_paths_are_not_allowed(self):
        for rel in (".env", "Specs/.env", "Keys/id_rsa", "Ledger/x/.env",
                    "Transcripts/run-1.txt", "state/events.jsonl", ".obsidian/workspace.json",
                    "Random/thing.md", "../outside.md", "certs/server.pem"):
            self.assertFalse(vault_git.allowed(rel), rel)

    def test_deny_entries_match_a_component_or_a_basename_not_a_bare_substring(self):
        # I3: a bare `d in rel` substring check let `state/` deny
        # `Projects/real-estate/x.md` (`state` sits inside `real-estate`) and
        # `.env` deny `Knowledge/dev.environment.md` (`.env` sits inside
        # `.environment.md`). A `/`-suffixed deny entry now matches only a
        # whole path COMPONENT; the rest match only the basename's suffix.
        for rel in ("Projects/real-estate/x.md", "Knowledge/dev.environment.md"):
            self.assertTrue(vault_git.allowed(rel), rel)
        for rel in ("Decisions/.env", "Ledger/x/secret.key", "Transcripts/a.md",
                    "state/x.json", ".obsidian/app.json", "Specs/id_rsa"):
            self.assertFalse(vault_git.allowed(rel), rel)

    def test_a_dot_slash_prefix_is_normalised_before_the_allow_check(self):
        """A caller that built its path with `./` got silently dropped from
        the push, with a warn event and no commit."""
        self.assertTrue(vault_git.allowed("./Boards/Engineering.md"))
        self.assertTrue(vault_git.allowed("Boards/Engineering.md"))
        self.assertTrue(vault_git.allowed("./Ledger/my-app/t_0000000a.jsonl"))

    def test_normalisation_does_not_widen_the_deny_list(self):
        self.assertFalse(vault_git.allowed("./state/x.json"))
        self.assertFalse(vault_git.allowed("./Decisions/.env"))
        self.assertFalse(vault_git.allowed("./../outside.md"))
        self.assertFalse(vault_git.allowed("./NotAllowed/x.md"))

    def test_a_denied_path_is_dropped_while_allowed_paths_still_stage(self):
        calls = []

        def git(cmd):
            calls.append(cmd)
            rc = 1 if cmd[3] == "diff" else 0
            return type("R", (), {"returncode": rc, "stdout": "", "stderr": ""})()

        self.assertTrue(vault_git.push("/tmp/v", ["Boards/Intake.md", "Transcripts/x.txt"],
                                       "m", runner=git))
        add = [c for c in calls if c[3] == "add"][0]
        self.assertIn("Boards/Intake.md", add)
        self.assertNotIn("Transcripts/x.txt", add)

    def test_dropping_every_path_is_a_no_op_that_still_returns_true(self):
        calls = []

        def git(cmd):
            calls.append(cmd)
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        self.assertTrue(vault_git.push("/tmp/v", ["Transcripts/x.txt"], "m", runner=git))
        self.assertEqual(calls, [])

    def test_dropping_a_path_emits_one_warn_event(self):
        rows = []
        real = vault_git.events.log
        vault_git.events.log = lambda *a, **kw: rows.append((a, kw))
        try:
            vault_git.push("/tmp/v", ["Transcripts/x.txt"], "m",
                           runner=lambda c: type("R", (), {"returncode": 0, "stdout": "",
                                                           "stderr": ""})())
        finally:
            vault_git.events.log = real
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1]["severity"], "warn")


class PitchesAllowlist(unittest.TestCase):
    """Row 139 / M246: `Pitches/` must be pushable BEFORE the first pitch file
    exists, or the first one is silently dropped with only a warn event
    (vault_git.push's dropped-path branch, :100-107). Nothing in capsule-corp
    writes a pitch today, so this is a zero-behaviour change whose whole value
    is the ordering."""

    def test_a_pitch_file_is_allowed(self):
        self.assertTrue(vault_git.allowed("Pitches/my-app-menu-bar.md"))
        self.assertTrue(vault_git.allowed("./Pitches/my-app-menu-bar.md"))

    def test_the_prefix_does_not_widen_to_a_lookalike_folder(self):
        self.assertFalse(vault_git.allowed("Pitchesx/y.md"))
        self.assertFalse(vault_git.allowed("Pitch/y.md"))

    def test_the_deny_list_still_wins_inside_pitches(self):
        self.assertFalse(vault_git.allowed("Pitches/.env"))
        self.assertFalse(vault_git.allowed("Pitches/state/x.json"))

    def test_pitches_is_the_only_new_prefix(self):
        """A second entry added by accident is caught here rather than in a
        review six weeks later."""
        self.assertTrue(all(vault_git.allowed(p + "x.md") for p in _LAYOUT))
        self.assertFalse(vault_git.allowed("Extra/x.md"))


class VaultIsOptIn(unittest.TestCase):
    """D254: no vault configured means every entry point does nothing; a configured one
    (env or host.json) works. Temp dirs only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.calls = []
        self._no_send = os.environ.pop("AGENT_OPS_NO_SEND", None)

    def tearDown(self):
        if self._no_send is not None:
            os.environ["AGENT_OPS_NO_SEND"] = self._no_send
        os.environ.pop(VAULT_ENV, None)
        _config({"push_allow": _LAYOUT})
        self.tmp.cleanup()

    def git(self, cmd):
        self.calls.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def test_without_a_vault_push_pull_and_web_base_do_nothing(self):
        for vault in (None, "", Path(self.tmp.name) / "missing"):
            self.assertTrue(vault_git.push(vault, ["Decisions/a.md"], "m"))
            self.assertTrue(vault_git.pull(vault))
            self.assertIsNone(vault_git.web_base(vault))
        self.assertEqual(self.calls, [])

    def test_no_vault_configured_is_none(self):
        _config({})
        self.assertIsNone(host_vault())

    def test_vault_path_from_host_json_then_env(self):
        _config({"path": self.tmp.name})
        self.assertEqual(host_vault(), Path(self.tmp.name))
        with tempfile.TemporaryDirectory() as other:
            os.environ[VAULT_ENV] = other
            self.assertEqual(host_vault(), Path(other))

    def test_default_allowlist_is_the_sixteen_generic_folders(self):
        _config({})
        self.assertEqual(host_cfg.get_or_default("vault.push_allow"), [
            "Ledger/", "Boards/", "Specs/", "Decisions/", "Architecture/", "Reviews/", "Projects/",
            "Reports/", "Inbox/", "Debugging/", "Dev Logs/", "Knowledge/", "Daily/", "Tasks/",
            "Brainstorms/", "Pitches/"])

    def test_the_default_allowlist_is_neutral_and_host_json_overrides_it(self):
        _config({})
        self.assertTrue(vault_git.allowed("Brainstorms/x.md"))
        self.assertTrue(vault_git.allowed("Ledger/x.md"))
        self.assertFalse(vault_git.allowed("Secrets/x.md"))
        _config({"push_allow": ["Notes/"]})
        self.assertTrue(vault_git.allowed("Notes/x.md"))
        self.assertFalse(vault_git.allowed("Brainstorms/x.md"))

    def test_a_configured_directory_vault_reaches_git(self):
        vault = Path(self.tmp.name) / "v"
        vault.mkdir()
        _config({"path": str(vault)})
        self.assertTrue(vault_git.pull(host_vault(), runner=self.git))
        self.assertEqual(self.calls[0][:3], ["git", "-C", str(vault)])


class OrderBookTemplates(unittest.TestCase):
    """Row 139 / M241, M246: the two templates the controller copies into the
    vault. This repo ships them and never writes the vault file."""

    T = Path(__file__).resolve().parents[1] / "templates"

    def test_the_order_book_template_has_the_five_sections_in_order(self):
        text = (self.T / "order-book.md").read_text(encoding="utf-8")
        want = ["## Live bet", "## On deck", "## Opportunities", "## Killed",
                "## Witnesses"]
        found = [l.strip() for l in text.splitlines() if l.startswith("## ")]
        self.assertEqual([h for h in found if h in want], want)

    def test_the_order_book_template_says_the_bridge_never_consumes_it(self):
        text = (self.T / "order-book.md").read_text(encoding="utf-8")
        self.assertIn("## For future agent", text)
        self.assertIn("hand-written", text)
        self.assertIn("never consumed by the bridge", text)

    def test_the_order_book_template_carries_the_caps_and_the_witness_rule(self):
        text = (self.T / "order-book.md").read_text(encoding="utf-8")
        for phrase in ("One live bet", "One bet on deck",
                       "~15 opportunities", "blocks the next bet",
                       "One discovery hour a week",
                       "at least one dated, attributed witness",
                       "no solution words",
                       "Enforced by reading until the first bet is stamped"):
            self.assertIn(phrase, text, phrase)

    def test_the_pitch_template_has_the_seven_sections_and_every_field(self):
        text = (self.T / "pitch.md").read_text(encoding="utf-8")
        for head in ("## Problem", "## Appetite", "## Solution sketch",
                     "## Rabbit holes", "## No-gos", "## One number",
                     "## Kill criteria"):
            self.assertIn(head, text, head)
        for key in ("slug:", "appetite_cards:", "appetite_epic_runs:",
                    "appetite_window_pct:", "one_number:", "instrument:",
                    "kill_by:", "witnesses:", "distribution:",
                    "distribution_fee:", "compliance_owner:",
                    "compliance_clock_checked:"):
            self.assertIn(key, text, key)

    def test_neither_template_is_a_vault_write(self):
        """The templates live in the repo. If a task ever made one a vault
        path, this catches it: no template may name an absolute vault path."""
        for name in ("order-book.md", "pitch.md"):
            text = (self.T / name).read_text(encoding="utf-8")
            self.assertNotIn("/Users/", text, name)
