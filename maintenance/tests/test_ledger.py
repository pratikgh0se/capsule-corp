# mutation-check: targets-predate-range b957739c9 -- rename only, no behaviour pinned ::
#   AppendAndRead.test_a_legacy_event_id_dedupes_on_its_own_key,
#   Entries.test_the_envelope_keys_are_exactly_the_spec_shape,
#   Keys.test_legacy_keyed_entries_key_on_the_event_id
import json, os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["AGENT_OPS_NO_SEND"] = "1"
os.environ.setdefault("EVENTS_SPOOL", str(Path(tempfile.mkdtemp(prefix="events-x-")) / "events.jsonl"))

import ledger
import importlib.util  # noqa: E402
# maintenance/tests is a package, so simplicity rule 1 does not count `fakes` as a bare repo
# module; load it by path, as test_e2e_pipeline does.
_FAKES_SPEC = importlib.util.spec_from_file_location(
    "fakes", Path(__file__).resolve().parent / "fakes.py")
fakes = importlib.util.module_from_spec(_FAKES_SPEC)
sys.modules["fakes"] = fakes
_FAKES_SPEC.loader.exec_module(fakes)
FakeLegacyCli = fakes.FakeLegacyCli
from capsule import legacy_names  # noqa: E402


def _host_json_names_vault(case):
    """Point CAPSULE_HOME at a scratch dir whose host.json names a scratch "real" vault (the
    machine's configured vault, never the owner's). Returns that vault path; undone on cleanup."""
    home = Path(tempfile.mkdtemp(prefix="ledger-host-"))
    real = home / "real-vault"
    real.mkdir()
    (home / "host.json").write_text(json.dumps({"vault": {"path": str(real)}}))
    old = os.environ.get("CAPSULE_HOME")
    os.environ["CAPSULE_HOME"] = str(home)
    case.addCleanup(lambda: os.environ.__setitem__("CAPSULE_HOME", old) if old is not None
                    else os.environ.pop("CAPSULE_HOME", None))
    return real


class VaultGuard(unittest.TestCase):
    """Ruling 1's matrix, one test per row. AGENT_OPS_NO_SEND is set for the
    whole module, so the two "marker set" rows are the default here and the one
    "marker unset" row clears it explicitly."""

    def setUp(self):
        self._old = os.environ.get("OBSIDIAN_VAULT_PATH")
        self._old_marker = os.environ.get("AGENT_OPS_NO_SEND")
        self.real = _host_json_names_vault(self)

    def tearDown(self):
        for name, old in (("OBSIDIAN_VAULT_PATH", self._old),
                          ("AGENT_OPS_NO_SEND", self._old_marker)):
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old

    def test_unset_is_a_silent_no_op_not_a_raise(self):
        os.environ.pop("OBSIDIAN_VAULT_PATH", None)
        self.assertIsNone(ledger.vault_root())

    def test_the_real_vault_is_refused_under_the_marker_even_when_exported(self):
        os.environ["OBSIDIAN_VAULT_PATH"] = str(self.real)
        with self.assertRaises(RuntimeError):
            ledger.vault_root()

    def test_the_real_vault_is_refused_when_passed_explicitly_too(self):
        with self.assertRaises(RuntimeError):
            ledger.vault_root(self.real)

    def test_without_the_marker_the_real_vault_is_allowed(self):
        os.environ.pop("AGENT_OPS_NO_SEND", None)
        os.environ["OBSIDIAN_VAULT_PATH"] = str(self.real)
        self.assertEqual(ledger.vault_root(), self.real)

    def test_a_temp_vault_is_allowed(self):
        tmp = tempfile.mkdtemp(prefix="ledger-vault-")
        os.environ["OBSIDIAN_VAULT_PATH"] = tmp
        self.assertEqual(ledger.vault_root(), Path(tmp))

    def test_a_missing_directory_is_none_not_a_raise(self):
        os.environ["OBSIDIAN_VAULT_PATH"] = str(Path(tempfile.mkdtemp()) / "gone")
        self.assertIsNone(ledger.vault_root())


class Entries(unittest.TestCase):
    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            ledger.make_entry("my-app", "t_1", "invented", source="x", dedupe="d")

    def test_dedupe_is_required(self):
        with self.assertRaises(TypeError):
            ledger.make_entry("my-app", "t_1", "created", source="x")

    def test_id_is_deterministic_32_hex(self):
        a = ledger.make_entry("my-app", "t_1", "created", source="intake", dedupe="k", ts=1.0)
        b = ledger.make_entry("my-app", "t_1", "created", source="intake", dedupe="k", ts=99.0)
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(a["id"]), 32)
        self.assertTrue(all(c in "0123456789abcdef" for c in a["id"]))

    def test_id_differs_per_card_and_per_dedupe(self):
        a = ledger.make_entry("my-app", "t_1", "created", source="intake", dedupe="k")
        b = ledger.make_entry("my-app", "t_2", "created", source="intake", dedupe="k")
        c = ledger.make_entry("my-app", "t_1", "created", source="intake", dedupe="j")
        self.assertNotEqual(a["id"], b["id"])
        self.assertNotEqual(a["id"], c["id"])

    def test_summary_is_truncated_on_a_word_boundary(self):
        long = ("word " * 80).strip()
        e = ledger.make_entry("my-app", "t_1", "merged", source="merge_gate",
                              dedupe="d", summary=long)
        self.assertLessEqual(len(e["summary"]), ledger.SUMMARY_CAP + 1)
        self.assertTrue(e["summary"].endswith("…"))
        self.assertFalse(e["summary"][:-1].endswith(" "))

    def test_a_short_summary_is_untouched(self):
        e = ledger.make_entry("my-app", "t_1", "merged", source="merge_gate",
                              dedupe="d", summary="merged pr 5")
        self.assertEqual(e["summary"], "merged pr 5")

    def test_control_bytes_are_scrubbed_from_summary_and_payload(self):
        e = ledger.make_entry("my-app", "t_1", "merged", source="merge_gate", dedupe="d",
                              summary="a\x00b", payload={"pr": "u\x07rl"})
        self.assertEqual(e["summary"], "ab")
        self.assertEqual(e["payload"]["pr"], "url")

    def test_an_unlisted_payload_key_is_dropped(self):
        e = ledger.make_entry("my-app", "t_1", "merged", source="merge_gate", dedupe="d",
                              payload={"pr": "u", "transcript": "SECRET", "lead_id": "t_9"})
        self.assertEqual(e["payload"], {"pr": "u", "lead_id": "t_9"})

    def test_a_non_scalar_payload_value_is_dropped(self):
        e = ledger.make_entry("my-app", "t_1", "merged", source="merge_gate", dedupe="d",
                              payload={"pr": {"nested": 1}, "lead_id": "t_9"})
        self.assertEqual(e["payload"], {"lead_id": "t_9"})

    def test_the_envelope_keys_are_exactly_the_spec_shape(self):
        e = ledger.make_entry("my-app", "t_1", "created", source="intake", dedupe="d")
        self.assertEqual(set(e), {"v", "id", "ts", "board", "task_id", "kind",
                                  "source", legacy_names.LEDGER_EVENT_KEY, "summary", "payload"})
        self.assertEqual(e["v"], ledger.LEDGER_V)
        self.assertIsNone(e[legacy_names.LEDGER_EVENT_KEY])


class Keys(unittest.TestCase):
    def test_legacy_keyed_entries_key_on_the_event_id(self):
        a = ledger.make_entry("p", "t_1", "changes_requested", source="bridge",
                              dedupe="changes:0", legacy_event_id=4172)
        b = ledger.make_entry("p", "t_1", "changes_requested", source="bridge",
                              dedupe="changes:99", legacy_event_id=4172)
        self.assertEqual(ledger.key(a), ledger.key(b))

    def test_id_keyed_entries_key_on_the_id(self):
        a = ledger.make_entry("p", "t_1", "merged", source="merge_gate", dedupe="x")
        b = ledger.make_entry("p", "t_1", "merged", source="merge_gate", dedupe="y")
        self.assertNotEqual(ledger.key(a), ledger.key(b))


class AppendAndRead(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("OBSIDIAN_VAULT_PATH")
        self.vault = Path(tempfile.mkdtemp(prefix="ledger-"))
        os.environ["OBSIDIAN_VAULT_PATH"] = str(self.vault)
        # _PENDING is a module-level counter incremented by any real append()
        # across the whole process; reset it here the same way OBSIDIAN_VAULT_PATH
        # is saved/restored, so this class's tests do not leak counts into
        # each other by alphabetical accident.
        ledger._PENDING = 0

    def tearDown(self):
        if self._old is None:
            os.environ.pop("OBSIDIAN_VAULT_PATH", None)
        else:
            os.environ["OBSIDIAN_VAULT_PATH"] = self._old

    def _entry(self, **kw):
        kw.setdefault("source", "test")
        kw.setdefault("dedupe", "d")
        return ledger.make_entry("my-app", "t_1", kw.pop("kind", "created"), **kw)

    def test_append_writes_one_line_and_returns_the_relative_path(self):
        rel = ledger.append("my-app", "t_1", self._entry())
        self.assertEqual(rel, "Ledger/my-app/t_1.jsonl")
        p = self.vault / rel
        self.assertEqual(len(p.read_text(encoding="utf-8").splitlines()), 1)

    def test_the_same_entry_twice_yields_one_line(self):
        ledger.append("my-app", "t_1", self._entry())
        self.assertIsNone(ledger.append("my-app", "t_1", self._entry()))
        p = self.vault / "Ledger" / "my-app" / "t_1.jsonl"
        self.assertEqual(len(p.read_text(encoding="utf-8").splitlines()), 1)

    def test_two_entries_differing_only_in_dedupe_yield_two_lines(self):
        ledger.append("my-app", "t_1", self._entry(dedupe="a"))
        ledger.append("my-app", "t_1", self._entry(dedupe="b"))
        p = self.vault / "Ledger" / "my-app" / "t_1.jsonl"
        self.assertEqual(len(p.read_text(encoding="utf-8").splitlines()), 2)

    def test_a_legacy_event_id_dedupes_on_its_own_key(self):
        ledger.append("my-app", "t_1", self._entry(kind="changes_requested",
                                                     dedupe="a", legacy_event_id=7))
        ledger.append("my-app", "t_1", self._entry(kind="changes_requested",
                                                     dedupe="b", legacy_event_id=7))
        p = self.vault / "Ledger" / "my-app" / "t_1.jsonl"
        self.assertEqual(len(p.read_text(encoding="utf-8").splitlines()), 1)

    def test_read_round_trips(self):
        ledger.append("my-app", "t_1", self._entry(dedupe="a"))
        ledger.append("my-app", "t_1", self._entry(dedupe="b"))
        got = ledger.read("my-app", "t_1")
        self.assertEqual([e["kind"] for e in got], ["created", "created"])

    def test_read_of_a_missing_file_is_empty(self):
        self.assertEqual(ledger.read("my-app", "t_nope"), [])

    def test_read_skips_a_corrupt_line(self):
        p = self.vault / "Ledger" / "my-app" / "t_1.jsonl"
        p.parent.mkdir(parents=True)
        p.write_text('{"v":1,"kind":"created"}\nnot json\n', encoding="utf-8")
        self.assertEqual(len(ledger.read("my-app", "t_1")), 1)

    def test_a_bad_board_or_task_id_never_escapes_the_ledger_dir(self):
        self.assertIsNone(ledger.append("../../etc", "t_1", self._entry()))
        self.assertIsNone(ledger.append("my-app", "../../etc/passwd", self._entry()))

    def test_an_unwritable_vault_returns_none(self):
        os.environ["OBSIDIAN_VAULT_PATH"] = str(self.vault / "does-not-exist")
        self.assertIsNone(ledger.append("my-app", "t_1", self._entry()))

    def test_pending_counts_only_real_appends(self):
        before = ledger.pending()
        ledger.append("my-app", "t_1", self._entry(dedupe="a"))
        ledger.append("my-app", "t_1", self._entry(dedupe="a"))
        self.assertEqual(ledger.pending(), before + 1)

    def test_push_pending_pushes_the_ledger_dir_and_resets(self):
        calls = []

        def git(cmd):
            calls.append(cmd)
            rc = 1 if cmd[3] == "diff" else 0     # "diff --cached --quiet" -> something staged
            return type("R", (), {"returncode": rc, "stdout": "", "stderr": ""})()

        ledger.append("my-app", "t_1", self._entry(dedupe="a"))
        self.assertTrue(ledger.push_pending(git_runner=git))
        staged = [c for c in calls if c[3] == "add"]
        self.assertEqual(staged[0][-1], "Ledger/")
        commit = [c for c in calls if c[3] == "commit"][0]
        self.assertIn("ledger: 1 entries", commit)
        self.assertEqual(ledger.pending(), 0)

    def test_a_failed_push_carries_the_count_to_the_next_tick(self):
        def failing_git(cmd):
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": "boom"})()

        ledger.append("my-app", "t_1", self._entry(dedupe="a"))
        before = ledger.pending()
        self.assertFalse(ledger.push_pending(git_runner=failing_git))
        self.assertEqual(ledger.pending(), before)

    def test_nothing_pending_and_no_ledger_dir_is_a_silent_skip(self):
        # C1: called every tick even when pending() == 0; before the first
        # ever entry, `Ledger/` does not exist and `git add -A -- Ledger/`
        # would fail with "pathspec did not match" (a warn every 10 minutes).
        calls = []

        def git(cmd):
            calls.append(cmd)
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        self.assertEqual(ledger.pending(), 0)
        self.assertFalse((self.vault / "Ledger").is_dir())
        self.assertFalse(ledger.push_pending(git_runner=git))
        self.assertEqual(calls, [])

    def test_nothing_pending_but_ledger_dir_present_still_pushes(self):
        # Existing behaviour preserved: once `Ledger/` exists, a no-op tick
        # still sweeps up out-of-tick CLI writes (`git diff --cached --quiet`
        # makes it free).
        calls = []

        def git(cmd):
            calls.append(cmd)
            rc = 1 if cmd[3] == "diff" else 0
            return type("R", (), {"returncode": rc, "stdout": "", "stderr": ""})()

        ledger.append("my-app", "t_1", self._entry(dedupe="a"))
        self.assertTrue(ledger.push_pending(git_runner=git))
        self.assertEqual(ledger.pending(), 0)
        self.assertTrue((self.vault / "Ledger").is_dir())
        calls.clear()
        self.assertTrue(ledger.push_pending(git_runner=git))
        self.assertTrue(calls)


class BoardHook(unittest.TestCase):
    """board.Board.append_ledger's never-raises contract still holds with the
    real hook wired (board.py:643-652), and an unrelated test that merely
    constructs a Board writes nothing (ruling 1's matrix, row 1)."""

    def setUp(self):
        self._old = os.environ.get("OBSIDIAN_VAULT_PATH")
        os.environ.pop("OBSIDIAN_VAULT_PATH", None)
        self.real = _host_json_names_vault(self)

    def tearDown(self):
        if self._old is not None:
            os.environ["OBSIDIAN_VAULT_PATH"] = self._old

    def test_append_ledger_is_a_no_op_when_the_vault_is_unset(self):
        import board
        b = board.open_board("my-app")
        self.assertIsNone(b.append_ledger("t_1", {"kind": "created"}))

    def test_append_ledger_never_raises_when_the_real_vault_is_refused(self):
        import board
        os.environ["OBSIDIAN_VAULT_PATH"] = str(self.real)
        b = board.open_board("my-app")
        e = ledger.make_entry("my-app", "t_1", "created", source="test", dedupe="d")
        self.assertIsNone(b.append_ledger("t_1", e))


class HookDefault(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("OBSIDIAN_VAULT_PATH")
        self.vault = Path(tempfile.mkdtemp(prefix="ledger-hook-"))
        os.environ["OBSIDIAN_VAULT_PATH"] = str(self.vault)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("OBSIDIAN_VAULT_PATH", None)
        else:
            os.environ["OBSIDIAN_VAULT_PATH"] = self._old

    def test_open_board_writes_through_the_real_hook_by_default(self):
        import board
        b = board.open_board("my-app")
        e = ledger.make_entry("my-app", "t_1", "created", source="test", dedupe="d")
        self.assertEqual(b.append_ledger("t_1", e), "Ledger/my-app/t_1.jsonl")
        self.assertEqual(len(ledger.read("my-app", "t_1")), 1)

    def test_no_ledger_is_still_the_explicit_opt_out(self):
        import board
        b = board.open_board("my-app", ledger=board.no_ledger)
        e = ledger.make_entry("my-app", "t_2", "created", source="test", dedupe="d")
        self.assertIsNone(b.append_ledger("t_2", e))
        self.assertEqual(ledger.read("my-app", "t_2"), [])

    def test_a_raising_hook_is_still_swallowed(self):
        import board

        def boom(*a):
            raise RuntimeError("nope")

        b = board.open_board("my-app", ledger=boom)
        self.assertIsNone(b.append_ledger("t_3", {"kind": "created"}))


class DispatchSites(unittest.TestCase):
    def setUp(self):
        import board_bridge as bb
        self._old = os.environ.get("OBSIDIAN_VAULT_PATH")
        self.vault = Path(tempfile.mkdtemp(prefix="ledger-dispatch-"))
        os.environ["OBSIDIAN_VAULT_PATH"] = str(self.vault)
        self.tmp = Path(tempfile.mkdtemp(prefix="ledger-dispatch-src-"))
        self.specs = self.tmp / "Specs"
        self.specs.mkdir()
        (self.specs / "soft-tick.md").write_text(
            "---\ntitle: Soft tick\nrepo: sample-app\nrisk: low\narea: frontend\n---\n"
            "# Soft tick\n## Goal\nA soft tick.\n", encoding="utf-8")
        self.h = FakeLegacyCli(self.tmp / "boards")
        self.bb = bb
        self._old_repo = bb.REPO_PATHS.get("sample-app")
        bb.REPO_PATHS["sample-app"] = str(self.tmp / "sample-app")

    def tearDown(self):
        if self._old_repo is None:
            self.bb.REPO_PATHS.pop("sample-app", None)
        else:
            self.bb.REPO_PATHS["sample-app"] = self._old_repo
        if self._old is None:
            os.environ.pop("OBSIDIAN_VAULT_PATH", None)
        else:
            os.environ["OBSIDIAN_VAULT_PATH"] = self._old

    def test_dispatch_writes_one_created_entry_per_card(self):
        impl_id, fin_id = self.bb.dispatch("soft-tick", specs_dir=self.specs, runner=self.h)
        kinds = [e["kind"] for e in ledger.read("sample-app", impl_id)]
        self.assertEqual(kinds, ["created"])
        self.assertEqual(ledger.read("sample-app", impl_id)[0]["payload"]["spec_slug"], "soft-tick")
        self.assertEqual([e["kind"] for e in ledger.read("sample-app", fin_id)], ["created"])

    def test_dispatch_twice_appends_nothing_the_second_time(self):
        impl_id, _ = self.bb.dispatch("soft-tick", specs_dir=self.specs, runner=self.h)
        self.bb.dispatch("soft-tick", specs_dir=self.specs, runner=self.h)
        self.assertEqual(len(ledger.read("sample-app", impl_id)), 1)


class GateSites(unittest.TestCase):
    """approved is ledgered where the stamp is first honoured; merged where the
    PR actually merges - both on the IMPL card's file (ruling 3/4)."""

    def setUp(self):
        self._old_vault = os.environ.get("OBSIDIAN_VAULT_PATH")
        self._old_state = os.environ.get("MERGE_GATE_STATE")
        self.vault = Path(tempfile.mkdtemp(prefix="ledger-gate-"))
        os.environ["OBSIDIAN_VAULT_PATH"] = str(self.vault)
        os.environ["MERGE_GATE_STATE"] = tempfile.mkdtemp(prefix="ledger-gate-state-")

    def tearDown(self):
        for name, old in (("OBSIDIAN_VAULT_PATH", self._old_vault),
                          ("MERGE_GATE_STATE", self._old_state)):
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old

    def test_a_merged_entry_from_two_observers_is_one_line(self):
        """merge_gate and sync_done both see the same merge; the dedupe seed is
        the PR url, so the second observation appends nothing."""
        a = ledger.make_entry("my-app", "t_1a2b", "merged", source="merge_gate",
                              summary="merged", dedupe="merged:https://x/pull/9",
                              payload={"pr": "https://x/pull/9", "lead_id": "t_fed1"})
        b = ledger.make_entry("my-app", "t_1a2b", "merged", source="bridge",
                              summary="Merged - https://x/pull/9.",
                              dedupe="merged:https://x/pull/9",
                              payload={"pr": "https://x/pull/9", "lead_id": "t_fed1"})
        self.assertEqual(ledger.key(a), ledger.key(b))
        self.assertIsNotNone(ledger.append("my-app", "t_1a2b", a))
        self.assertIsNone(ledger.append("my-app", "t_1a2b", b))

    def test_a_second_approval_after_a_changes_round_is_a_second_entry(self):
        a = ledger.make_entry("my-app", "t_1a2b", "approved", source="merge_gate",
                              dedupe="approved:https://x/pull/9:1000",
                              payload={"pr": "https://x/pull/9", "via": "stamp:host"})
        b = ledger.make_entry("my-app", "t_1a2b", "approved", source="merge_gate",
                              dedupe="approved:https://x/pull/9:2000",
                              payload={"pr": "https://x/pull/9", "via": "stamp:host"})
        self.assertIsNotNone(ledger.append("my-app", "t_1a2b", a))
        self.assertIsNotNone(ledger.append("my-app", "t_1a2b", b))
        self.assertEqual(len(ledger.read("my-app", "t_1a2b")), 2)


if __name__ == "__main__":
    unittest.main()
