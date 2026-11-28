"""log.md rotation (row 128, M078). Rotation ONLY - nothing else on the
archival calendar moves. Fixture vaults; the real log.md is never touched."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import log_rotate

LOG = """# Operation Log

Append-only log of automated consolidation and maintenance passes.

## [2026-03-02] nightly | one

- did a thing

## [2026-06-15] nightly | two

- did another

## [2026-09-09] nightly | three

- did a third
"""


class Rotate(unittest.TestCase):
    def _vault(self, td):
        v = Path(td); (v / "log.md").write_text(LOG, encoding="utf-8"); return v

    def test_a_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            v = self._vault(td)
            before = (v / "log.md").read_text()
            res = log_rotate.rotate(v, before="2026-07-01")
            self.assertEqual((v / "log.md").read_text(), before)
            self.assertFalse((v / "_archive").exists())
            self.assertEqual(res["entries_to_move"], 2)

    def test_the_rollup_is_written_before_the_lines_move(self):
        with tempfile.TemporaryDirectory() as td:
            v = self._vault(td)
            res = log_rotate.rotate(v, before="2026-07-01", apply=True)
            self.assertEqual(res["order"], ["rollup", "archive", "trim"])
            for q in ("2026-Q1", "2026-Q2"):
                self.assertTrue((v / "_archive" / f"log-{q}.md").is_file(), q)

    def test_the_rollup_note_names_the_period_and_the_entry_count(self):
        with tempfile.TemporaryDirectory() as td:
            v = self._vault(td)
            log_rotate.rotate(v, before="2026-07-01", apply=True)
            text = (v / "Knowledge" / "Operation Log Rollup 2026-Q1.md").read_text()
            self.assertIn("## For future agent", text)
            self.assertIn("2026-Q1", text)
            self.assertIn("1 entry", text)

    def test_recent_entries_stay_in_log_md(self):
        with tempfile.TemporaryDirectory() as td:
            v = self._vault(td)
            log_rotate.rotate(v, before="2026-07-01", apply=True)
            live = (v / "log.md").read_text()
            self.assertIn("2026-09-09", live)
            self.assertNotIn("did a thing", live)

    def test_log_md_keeps_a_twenty_line_index_of_what_moved(self):
        with tempfile.TemporaryDirectory() as td:
            v = self._vault(td)
            log_rotate.rotate(v, before="2026-07-01", apply=True)
            live = (v / "log.md").read_text()
            self.assertIn("_archive/log-2026-Q1.md", live)
            self.assertLessEqual(len([l for l in live.splitlines()
                                      if l.startswith("- [2026-Q")]), 20)

    def test_a_second_run_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as td:
            v = self._vault(td)
            log_rotate.rotate(v, before="2026-07-01", apply=True)
            res = log_rotate.rotate(v, before="2026-07-01", apply=True)
            self.assertEqual(res["entries_to_move"], 0)

    def test_rotate_twice_appends_each_quarter_once(self):
        with tempfile.TemporaryDirectory() as td:
            v = self._vault(td)
            before_bytes = (v / "log.md").read_bytes()
            res = log_rotate.rotate(v, before="2026-07-01", apply=True)
            self.assertEqual(res["quarters"], ["2026-Q1", "2026-Q2"])
            # Hand-append a line, as a human editing the archive directly would.
            for q in ("2026-Q1", "2026-Q2"):
                archive = v / "_archive" / f"log-{q}.md"
                archive.write_text(archive.read_text(encoding="utf-8")
                                    + "\nnote from a human\n", encoding="utf-8")
            # Restore log.md to its pre-rotation bytes and rotate again.
            (v / "log.md").write_bytes(before_bytes)
            log_rotate.rotate(v, before="2026-07-01", apply=True)
            for q, needle in (("2026-Q1", "did a thing"), ("2026-Q2", "did another")):
                text = (v / "_archive" / f"log-{q}.md").read_text(encoding="utf-8")
                self.assertEqual(text.count(needle), 1, q)
                self.assertIn("note from a human", text)

    def test_legacy_archive_without_marker_is_not_duplicated(self):
        with tempfile.TemporaryDirectory() as td:
            v = self._vault(td)
            header, existing_index, entries = log_rotate._split(
                (v / "log.md").read_text())
            import datetime
            cutoff = datetime.date.fromisoformat("2026-04-01")
            old = [e for e in entries
                   if datetime.date.fromisoformat(e["date"]) < cutoff]
            by_q = {}
            for e in old:
                by_q.setdefault(e["quarter"], []).append(e)
            # Write the archive the OLD way: no marker, just the block.
            (v / "_archive").mkdir(parents=True, exist_ok=True)
            body = "\n\n".join(log_rotate._entry_text(e) for e in old)
            (v / "_archive" / "log-2026-Q1.md").write_text(
                f"# Operation Log - 2026-Q1\n\n{body}\n", encoding="utf-8")
            log_rotate._write_archive(v, by_q)
            text = (v / "_archive" / "log-2026-Q1.md").read_text(encoding="utf-8")
            self.assertEqual(text.count("did a thing"), 1)

    def test_a_second_rotation_with_a_different_before_merges_into_one_archived_section(self):
        with tempfile.TemporaryDirectory() as td:
            v = self._vault(td)
            header_before = (v / "log.md").read_text().split("## [2026-03-02")[0]
            log_rotate.rotate(v, before="2026-04-01", apply=True)  # moves only Q1
            res = log_rotate.rotate(v, before="2026-07-01", apply=True)  # moves Q2
            self.assertEqual(res["entries_to_move"], 1)
            live = (v / "log.md").read_text()
            self.assertEqual(live.count("## Archived"), 1)
            self.assertIn("[2026-Q1]", live)
            self.assertIn("[2026-Q2]", live)
            self.assertTrue((v / "_archive" / "log-2026-Q1.md").is_file())
            self.assertTrue((v / "_archive" / "log-2026-Q2.md").is_file())
            # the true header (title + prose) is unchanged, not duplicated
            self.assertEqual(live.split("## [2026-09-09")[0].count(
                "Append-only log of automated consolidation"), 1)
            self.assertTrue(live.startswith(header_before.rstrip()))

    def test_exactly_one_blank_line_between_kept_entries_after_trim(self):
        with tempfile.TemporaryDirectory() as td:
            v = self._vault(td)
            # before="2026-04-01" moves only Q1, leaving two live entries
            # (2026-06-15 and 2026-09-09) so the between-entries gap is
            # actually exercised, not just the gap after the index block.
            log_rotate.rotate(v, before="2026-04-01", apply=True)
            live = (v / "log.md").read_text()
            self.assertNotIn("\n\n\n", live)
            gap = live[live.index("- did another") + len("- did another"):
                       live.index("## [2026-09-09")]
            self.assertEqual(gap, "\n\n")

    def test_a_second_rotation_gap_stays_single_blank_too(self):
        with tempfile.TemporaryDirectory() as td:
            v = self._vault(td)
            log_rotate.rotate(v, before="2026-04-01", apply=True)
            log_rotate.rotate(v, before="2026-07-01", apply=True)
            live = (v / "log.md").read_text()
            self.assertNotIn("\n\n\n", live)

    def test_a_simulated_crash_between_archive_and_trim_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as td:
            v = self._vault(td)
            header, existing_index, entries = log_rotate._split(
                (v / "log.md").read_text())
            import datetime
            cutoff = datetime.date.fromisoformat("2026-07-01")
            old = [e for e in entries
                   if datetime.date.fromisoformat(e["date"]) < cutoff]
            by_q = {}
            for e in old:
                by_q.setdefault(e["quarter"], []).append(e)

            # Simulate a crash right after the archive step, before the trim.
            log_rotate._write_rollups(v, by_q)
            log_rotate._write_archive(v, by_q)
            after_crash = (v / "_archive" / "log-2026-Q1.md").read_text()
            self.assertFalse((v / "log.md").read_text() == "")  # log.md untouched

            # Recovery: log.md still lists the entries as live, so a plain
            # re-run recomputes the same work and must not duplicate it.
            res = log_rotate.rotate(v, before="2026-07-01", apply=True)
            self.assertEqual(res["entries_to_move"], 2)
            after_recovery = (v / "_archive" / "log-2026-Q1.md").read_text()
            self.assertEqual(after_crash, after_recovery)
            self.assertEqual(after_recovery.count("did a thing"), 1)

    def test_the_archive_folder_is_excluded_from_retrieval(self):
        """The point of doing log.md FIRST: it exercises the `_archive`
        exclusion once, on content with no recall value, before anything that
        matters moves. `_archive` has been in INDEX_POLICY.deny_dir_names and
        _LEXICAL_DENY_DIR_NAMES since rows 120/124 and has never matched a
        single path, because the folder did not exist."""
        import importlib.util, os
        import vault_conf
        skill_root = vault_conf.skill_root()
        if skill_root is None:
            self.skipTest("no skill checkout configured")
        spec_path = skill_root / "scripts" / "eval" / "semantic_search.py"
        if not spec_path.is_file():
            self.skipTest("fork not present")
        text = spec_path.read_text(encoding="utf-8")
        self.assertIn('"_archive"', text)
        vo = skill_root / "integrations" / "obsidian-mcp-server" / "vault_ops.py"
        # Same skip as above (final review I11): a present-but-restructured
        # fork, or a partial checkout, must SKIP - a missing external checkout
        # is never a test error in this suite.
        if not vo.is_file():
            self.skipTest("fork present but vault_ops.py not at the expected path")
        self.assertIn('"_archive"', vo.read_text(encoding="utf-8"))
