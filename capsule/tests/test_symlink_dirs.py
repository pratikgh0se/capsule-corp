"""W234: host writers never follow a symlinked parent dir or a planted temp
(safe_write, spool, Korin note)."""
import contextlib, os, tempfile, unittest
from pathlib import Path
from unittest import mock

from capsule import safe_write, spool, tick


class Base(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name) / "root"
        self.root.mkdir()
        self.outside = Path(td.name) / "outside"
        self.outside.mkdir()

    def assertOutsideEmpty(self):
        self.assertEqual(list(self.outside.iterdir()), [])


class SafeWrite(Base):
    def test_normal_write_and_create_dirs(self):
        n = safe_write.atomic_write(self.root, self.root / "a" / "b.txt", "hi", create_dirs=True)
        self.assertEqual(n, 2)
        self.assertEqual((self.root / "a" / "b.txt").read_text(), "hi")
        self.assertEqual([p.name for p in (self.root / "a").iterdir()], ["b.txt"])

    def test_symlinked_parent_refused(self):
        os.symlink(self.outside, self.root / "sub")
        with self.assertRaises(safe_write.SymlinkRefused):
            safe_write.atomic_write(self.root, self.root / "sub" / "f.txt", "x", create_dirs=True)
        self.assertOutsideEmpty()

    def test_missing_parent_without_create_fails(self):
        with self.assertRaises(OSError):
            safe_write.atomic_write(self.root, self.root / "nope" / "f.txt", "x")

    def test_path_outside_root_refused(self):
        with self.assertRaises(OSError):
            safe_write.atomic_write(self.root, self.outside / "f.txt", "x")

    def test_symlinked_target_replaced_not_followed(self):
        victim = self.outside / "v.txt"
        victim.write_text("precious")
        os.symlink(victim, self.root / "f.txt")
        safe_write.atomic_write(self.root, self.root / "f.txt", "new")
        self.assertEqual(victim.read_text(), "precious")
        self.assertFalse((self.root / "f.txt").is_symlink())

    def test_failure_removes_only_own_temp(self):
        (self.root / "f.txt").write_text("old")
        with mock.patch.object(safe_write.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                safe_write.atomic_write(self.root, self.root / "f.txt", "new")
        self.assertEqual([p.name for p in self.root.iterdir()], ["f.txt"])
        self.assertEqual((self.root / "f.txt").read_text(), "old")


class SpoolWriter(Base):
    def test_normal_write(self):
        (self.root / "requests").mkdir()
        p = spool.write_doc(self.root, "requests", {"a": 1})
        self.assertEqual(p.name, "1.json")
        self.assertEqual([x.name for x in (self.root / "requests").iterdir()], ["1.json"])

    def test_symlinked_sub_dir_not_written_through(self):
        os.symlink(self.outside, self.root / "requests")
        with self.assertRaises(OSError):
            spool.write_doc(self.root, "requests", {"a": 1})
        self.assertOutsideEmpty()

    def test_planted_temp_symlink_not_written_through(self):
        (self.root / "requests").mkdir()
        victim = self.outside / "v.txt"
        victim.write_text("precious")
        os.symlink(victim, self.root / "requests" / ".1.json.tmp")
        spool.write_doc(self.root, "requests", {"a": 1})
        self.assertEqual(victim.read_text(), "precious")
        self.assertTrue((self.root / "requests" / "1.json").is_file())


class KorinNote(Base):
    def run_close(self, vault, rel, fix_card=None):
        inc = {"id": "i1", "fix_card": fix_card, "task_id": "t0", "kind": "crash"}
        with contextlib.ExitStack() as st:
            m = st.enter_context(mock.patch.object(tick, "incidents"))
            for name in ("txn", "lease", "db", "event", "outbox"):
                st.enter_context(mock.patch.object(tick, name))
            tick.txn.side_effect = lambda c: contextlib.nullcontext()
            m.get.return_value = inc
            m.postmortem_problems.return_value = []
            m.settings.return_value = {"vault": vault, "vault_push": False, "watch_days": 14}
            m.note_rel.return_value = rel
            m.fix_route.return_value = (None, None)
            m.render_note.return_value = "NOTE"
            obj = mock.MagicMock()
            report = {"fix_card": {"title": "t", "body": "b"}, "decision_row_text": "d",
                      "root_cause": "r", "mitigation": "m"}
            obj._report.return_value = (report, "")
            run = {"metadata": '{"incident": "i1"}', "task_id": "t1", "id": 1, "run_id": "r1"}
            tick.Tick._close_korin(obj, mock.MagicMock(), {"board": "b"}, run, mock.MagicMock(),
                                   {"x": 1}, None, 0)
            return tick.outbox.notify.call_args_list

    def test_note_written_inside_vault(self):
        self.run_close(self.root, "Notes/n.md")
        self.assertEqual((self.root / "Notes" / "n.md").read_text(), "NOTE")

    def test_symlinked_note_dir_not_written_through(self):
        os.symlink(self.outside, self.root / "Notes")
        calls = self.run_close(self.root, "Notes/n.md")
        self.assertOutsideEmpty()
        self.assertTrue(any("not written" in str(c) for c in calls))

    def test_planted_temp_symlink_not_written_through(self):
        (self.root / "Notes").mkdir()
        victim = self.outside / "v.txt"
        victim.write_text("precious")
        os.symlink(victim, self.root / "Notes" / ".n.md.tmp")
        self.run_close(self.root, "Notes/n.md")
        self.assertEqual(victim.read_text(), "precious")
        self.assertEqual((self.root / "Notes" / "n.md").read_text(), "NOTE")


if __name__ == "__main__":
    unittest.main()
