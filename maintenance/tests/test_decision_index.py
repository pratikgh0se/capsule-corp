"""The decision log's generated index and per-domain views (row 128, M070).
Fixture text only, temp dirs only: the real log and the live vault are never
touched by this suite."""
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import decision_log as dl

TABLE = """## Decisions

| # | Topic | Decision | Status |
|---|---|---|---|
| 1 | C4 Memory and retrieval tick table (owner, 2026-09-09 21:05) | Retrieval contract | decided |
| 2 | Slack: name and the four open questions | The name stays | build now |
| 3 | 2026-09-29 10:50 | A dated row with no topic words | Fable |
| 4 | Board truth Q9 - one board module | One module | decided |
| 5 | C4 amendment | Refines row 1 | decided |
"""


def _rows(text=TABLE):
    return dl.parse(text)


class Domain(unittest.TestCase):
    def test_a_leading_cluster_tag_is_the_domain(self):
        self.assertEqual(dl.domain_of({"title": "C4 Memory and retrieval", "text": ""}), "c4")
        self.assertEqual(dl.domain_of({"title": "C11 something", "text": ""}), "c11")

    def test_a_keyword_in_the_title_picks_the_domain(self):
        self.assertEqual(dl.domain_of({"title": "Slack: name", "text": ""}), "messaging")

    def test_a_private_project_name_has_no_domain_of_its_own(self):
        # Driven by the private denylist, so no private name is typed in this file.
        denylist = Path(__file__).resolve().parents[2] / "release" / "private-denylist.txt"
        if not denylist.is_file():
            self.skipTest("release/private-denylist.txt is absent (public checkout)")
        import public_lint
        rules, _ = public_lint._load_denylist(denylist)
        names = [n for category, rx, _ in rules if category == "project"
                 for n in rx.split("|") if re.fullmatch(r"[a-z][a-z-]*", n)]
        self.assertTrue(names, "denylist has no project names to check")
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(
                    dl.domain_of({"number": 7, "title": f"{name}: name", "text": ""}),
                    "general-001-100")

    def test_anything_else_is_general(self):
        self.assertEqual(dl.domain_of({"number": 7, "title": "2026-09-29 10:50", "text": "x"}),
                         "general-001-100")

    def test_the_domain_is_a_pure_function_of_the_row(self):
        row = _rows()[1]
        self.assertEqual(dl.domain_of(row), dl.domain_of(dict(row)))


class Index(unittest.TestCase):
    def _lines(self, rows):
        return [l for l in dl._render_index(rows).splitlines() if re.match(r"^- \d+ \|", l)]

    def test_one_line_per_row_with_number_topic_status_and_tag(self):
        lines = self._lines(_rows())
        self.assertEqual(len(lines), 5)
        self.assertEqual(lines[0], "- 1 | C4 Memory and retrieval tick table | decided | #c4")
        self.assertEqual(lines[1],
                         "- 2 | Slack: name and the four open questions | build now | #messaging")

    def test_a_row_with_no_status_shows_a_dash(self):
        rows = dl.parse("| 7 | 2026-10-01 | text only |\n")
        self.assertIn("- 7 | 2026-10-01 | - | #general-001-100", dl._render_index(rows))

    def test_a_duplicate_number_gets_a_line_each_so_it_stays_visible(self):
        dup = TABLE + "| 5 | Second five | x | decided |\n"
        nums = [int(re.match(r"- (\d+)", l).group(1)) for l in self._lines(_rows(dup))]
        self.assertEqual(nums, [1, 2, 3, 4, 5, 5])

    def test_the_index_says_it_is_generated_and_the_table_is_the_authority(self):
        text = dl._render_index(_rows())
        self.assertIn("generated index; the authority is the decisions table", text)
        self.assertIn("## For future agent", text)

    def test_the_index_wikilinks_every_view_so_none_is_an_orphan(self):
        text = dl._render_index(_rows())
        links = re.findall(r"^- \[\[(.+?)\]\]$", text, re.M)
        self.assertEqual(links, ["Decisions - board", "Decisions - c4",
                                 "Decisions - general-001-100", "Decisions - messaging"])
        self.assertEqual(sorted(f"Decisions - {d}" for d in dl._render_views(_rows())), links)

    def test_the_render_is_byte_stable(self):
        self.assertEqual(dl._render_index(_rows()), dl._render_index(_rows()))

    def test_input_order_does_not_change_the_bytes(self):
        rows = _rows()
        self.assertEqual(dl._render_index(rows), dl._render_index(list(reversed(rows))))


class Views(unittest.TestCase):
    def test_views_partition_the_rows(self):
        views = dl._render_views(_rows())
        self.assertEqual(sorted(views), ["board", "c4", "general-001-100", "messaging"])
        found = sorted(int(n) for t in views.values() for n in re.findall(r"^### (\d+) ", t, re.M))
        self.assertEqual(found, [1, 2, 3, 4, 5])

    def test_each_view_is_headed_generated_and_carries_the_row_text(self):
        views = dl._render_views(_rows())
        for domain, text in views.items():
            self.assertIn("generated view; the authority is the decisions table", text, domain)
        self.assertIn("Retrieval contract", views["c4"])
        self.assertIn("### 2 - Slack: name and the four open questions", views["messaging"])

    def test_the_render_is_byte_stable(self):
        self.assertEqual(dl._render_views(_rows()), dl._render_views(_rows()))


class Write(unittest.TestCase):
    def _write(self, out, rows=None):
        return dl._write_index_and_views(out, _rows() if rows is None else rows)

    def test_writes_index_and_one_file_per_domain(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(self._write(d))
            self.assertTrue((Path(d) / "Decision index.md").is_file())
            self.assertEqual(sorted(p.name for p in (Path(d) / "views").glob("*.md")),
                             ["Decisions - board.md", "Decisions - c4.md",
                              "Decisions - general-001-100.md", "Decisions - messaging.md"])

    def test_no_generated_name_can_collide_with_a_real_vault_note(self):
        """views/messaging.md would shadow Projects/Messaging.md, index.md the vault root's."""
        with tempfile.TemporaryDirectory() as d:
            self._write(d)
            names = [p.stem.lower() for p in Path(d).rglob("*.md")]
            self.assertNotIn("index", names)
            self.assertNotIn("messaging", names)
            ok = all(n == "decision index" or n.startswith("decisions - ") for n in names)
            self.assertTrue(ok)

    def test_an_existing_file_that_is_not_generated_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            mine = Path(d) / "Decision index.md"
            mine.write_text("# my own note\n", encoding="utf-8")
            (Path(d) / "views").mkdir()
            theirs = Path(d) / "views" / "Decisions - c4.md"
            theirs.write_text("hand written\n", encoding="utf-8")
            self.assertTrue(self._write(d))  # the other views still get written
            self.assertEqual(mine.read_text(encoding="utf-8"), "# my own note\n")
            self.assertEqual(theirs.read_text(encoding="utf-8"), "hand written\n")
            self.assertTrue((Path(d) / "views" / "Decisions - board.md").is_file())

    def test_the_general_bucket_is_sharded_by_blocks_of_100_rows(self):
        names = {dl.domain_of({"number": n, "title": "x"}) for n in (1, 100, 101, 250, 623)}
        self.assertEqual(names, {"general-001-100", "general-101-200", "general-201-300",
                                 "general-601-700"})
        self.assertEqual(dl.domain_of({"number": 150, "title": "Slack x"}), "messaging")

    def test_identical_input_writes_no_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d)
            before = {p: p.stat().st_mtime_ns for p in Path(d).rglob("*.md")}
            self.assertFalse(self._write(d))
            self.assertEqual(before, {p: p.stat().st_mtime_ns for p in Path(d).rglob("*.md")})

    def test_a_vanished_domain_loses_its_stale_generated_view(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d)
            kept = [r for r in _rows() if r["number"] != 2]  # the only messaging row
            self.assertTrue(self._write(d, kept))
            self.assertFalse((Path(d) / "views" / "Decisions - messaging.md").exists())

    def test_a_hand_written_file_in_views_is_never_deleted(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "views").mkdir()
            for name in ("notes.md", "Decisions - old.md"):  # the second has our prefix, no marker
                (Path(d) / "views" / name).write_text("mine\n", encoding="utf-8")
            self._write(d)
            for name in ("notes.md", "Decisions - old.md"):
                self.assertEqual((Path(d) / "views" / name).read_text(encoding="utf-8"), "mine\n")


class CheckViews(unittest.TestCase):
    def test_clean_output_has_no_problems(self):
        with tempfile.TemporaryDirectory() as d:
            dl._write_index_and_views(d, _rows())
            self.assertEqual(dl._check_views(_rows(), d), [])

    def test_a_view_row_absent_from_the_table_is_a_problem(self):
        with tempfile.TemporaryDirectory() as d:
            dl._write_index_and_views(d, _rows())
            kept = [r for r in _rows() if r["number"] != 4]
            problems = dl._check_views(kept, d)
            self.assertTrue(any("4" in p and "board" in p for p in problems), problems)
            self.assertTrue(any("index" in p for p in problems), problems)

    def test_missing_output_is_not_a_problem_yet(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(dl._check_views(_rows(), d), [])


class Cli(unittest.TestCase):
    def _vault(self, d):
        dec = Path(d) / "Decisions"
        dec.mkdir()
        name = "Capsule Corp - System design decisions 2026-09-06.md"
        (dec / name).write_text(TABLE, encoding="utf-8")
        return str(d)

    def test_the_sidecar_verb_also_regenerates_index_and_views_in_the_vault(self):
        with tempfile.TemporaryDirectory() as d:
            vault = self._vault(d)
            self.assertEqual(dl.main(["sidecar", vault]), 0)
            self.assertTrue((Path(vault) / "Decisions" / "decisions.jsonl").is_file())
            dec = Path(vault) / "Decisions"
            self.assertTrue((dec / "Decision index.md").is_file())
            self.assertTrue((dec / "views" / "Decisions - c4.md").is_file())

    def test_there_is_no_flag_that_redirects_the_write_elsewhere(self):
        """The nightly allow rule is `decision_log.py sidecar *`: an output-dir
        flag would hand the nightly model a write path into any directory."""
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
            vault = self._vault(d)
            dl.main(["sidecar", vault, "--out", out])
            self.assertEqual(list(Path(out).iterdir()), [])
            self.assertNotIn("--out", Path(dl.__file__).read_text(encoding="utf-8"))

    def test_the_vault_job_health_reports_a_view_that_names_a_lost_row(self):
        import json
        import vault_jobs as vj
        with tempfile.TemporaryDirectory() as d:
            vault = self._vault(d)
            dl.main(["sidecar", vault])
            row4 = "| 4 | Board truth Q9 - one board module | One module | decided |\n"
            dl.find_log(vault).write_text(TABLE.replace(row4, ""), encoding="utf-8")
            today = vj.dt.date(2026, 9, 6)
            line = vj.health(vault, today=today,
                             runner=lambda argv: json.dumps({"total_notes": 1, "issues": []}))
            health = Path(vault) / "Knowledge" / "Vault Health 2026-09-06.md"
            note = health.read_text(encoding="utf-8")
            self.assertIn("2 issues", line)  # the index and the board view
            self.assertIn("view Decisions - board names row(s) 4", note)

    def test_lint_flags_a_view_row_the_table_no_longer_has(self):
        with tempfile.TemporaryDirectory() as d:
            vault = self._vault(d)
            dl.main(["sidecar", vault])
            log = dl.find_log(vault)
            row4 = "| 4 | Board truth Q9 - one board module | One module | decided |\n"
            log.write_text(TABLE.replace(row4, ""), encoding="utf-8")
            self.assertEqual(dl.main(["lint", vault]), 1)


if __name__ == "__main__":
    unittest.main()
