import os, sys, io, json, contextlib, unittest, tempfile, datetime as dt, subprocess
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vault_jobs as vj

TODAY = dt.date(2026, 9, 6)

def tmp_vault():
    v = Path(tempfile.mkdtemp(prefix="vault-jobs-"))
    for d in ("Knowledge", "Daily", "Projects", "Boards", "Templates", "Ideas", "Inbox"):
        (v / d).mkdir()
    return v


class Frontmatter(unittest.TestCase):
    def test_parse(self):
        fm, body = vj.frontmatter('---\ndate: 2026-09-06\ntype: project\ntags:\n  - a\nstatus: "active"\n---\n# T\nbody')
        self.assertEqual(fm, {"date": "2026-09-06", "type": "project", "status": "active"})
        self.assertEqual(body, "# T\nbody")

    def test_no_frontmatter(self):
        self.assertEqual(vj.frontmatter("hello"), ({}, "hello"))


class Board(unittest.TestCase):
    TEXT = "## 📥 Backlog\n\n- [ ] 🟡 **Card A** · @{2026-09-01} #sprint\n\tbody\n- [ ] **Card B** 📅 2026-09-05\n\n## ✅ Done\n\n- [x] ~~🟢 **Old** · @{2026-09-01}~~\n"
    def test_columns_and_titles(self):
        cols = vj.board_columns(self.TEXT)
        self.assertEqual(list(cols), ["📥 Backlog", "✅ Done"])
        self.assertEqual([vj.card_title(l) for l in cols["📥 Backlog"]], ["Card A", "Card B"])
        self.assertEqual(vj.card_title(cols["✅ Done"][0]), "Old")

    def test_the_due_grammar_is_gone(self):
        """3.4: the generated board carries no `📅`, so a due date could only
        ever come from a hand-written line - and there are none left."""
        for name in ("due_date", "due_cards", "DUE_RE", "DUE_ROW_RE", "DUE_HEAD"):
            self.assertFalse(hasattr(vj, name), name)

    def test_a_legacy_due_marker_is_still_stripped_from_a_title(self):
        """Ruling 12: card_title runs over HISTORICAL revisions of the board
        from git (`board_at`), which still contain hand-written `📅` lines."""
        self.assertEqual(vj.card_title("- [ ] **Card B** 📅 2026-09-05"), "Card B")

    def test_title_with_nested_bold(self):
        line = '- [ ] **Release `**Gate:** after "<card>" is Done` lines automatically**'
        self.assertEqual(vj.card_title(line), 'Release `**Gate:** after "<card>" is Done` lines automatically')
        self.assertEqual(vj.card_title("- [ ] **Card A** · @{2026-09-01} #sprint"), "Card A")

    def test_title_stops_at_first_bold_span(self):
        line = "- [ ] **Card A** **Card B** · @{2026-09-01} #sprint"
        self.assertEqual(vj.card_title(line), "Card A")

    def test_title_strikethrough_done(self):
        self.assertEqual(vj.card_title("- [x] ~~**Old**~~"), "Old")


class Health(unittest.TestCase):
    DATA = {"total_notes": 10, "total_issues": 3, "counts": {}, "issues": [
        {"type": "orphan", "severity": "warning", "message": "Orphans", "files": ["a.md", "b.md"]},
        {"type": "dup", "severity": "info", "message": "Same title", "files": ["copilot/x.md"]},
        {"type": "fm", "severity": "critical", "message": "Missing frontmatter", "files": ["c.md"]}]}

    def test_filter_copilot(self):
        self.assertEqual([i["type"] for i in vj.filter_copilot(self.DATA["issues"])], ["orphan", "fm"])

    def test_render_first_check(self):
        out = vj.render_health({**self.DATA, "issues": vj.filter_copilot(self.DATA["issues"])}, TODAY, None)
        self.assertTrue(out.startswith("---\ndate: 2026-09-06\ntype: vault-health\n"))
        self.assertIn("total_issues: 2\n", out)
        self.assertIn("## Critical\n- Missing frontmatter (1 files)\n", out)
        self.assertIn("## Warning\n- Orphans (2 files)\n", out)
        self.assertNotIn("## Info", out)
        self.assertIn("## Trend\nFirst health check - no prior trend.", out)

    def test_trend_and_prior(self):
        v = tmp_vault()
        (v / "Knowledge" / "Vault Health 2026-08-30.md").write_text("---\ntype: vault-health-report\nscanned: 2026-08-30\ntotal_notes: 5\ntotal_issues: 1\n---\n")
        self.assertEqual(vj.prior_health(v, TODAY), (1, "2026-08-30"))
        out = vj.render_health({**self.DATA, "issues": vj.filter_copilot(self.DATA["issues"])}, TODAY, (1, "2026-08-30"))
        self.assertIn("## Trend\n2 issues, up from 1 on 2026-08-30", out)
        (v / "Knowledge" / "Vault Health 2026-09-06.md").write_text("x")   # today's own note is never the prior
        self.assertEqual(vj.prior_health(v, TODAY), (1, "2026-08-30"))

    def test_prior_legacy_summary_line(self):
        v = tmp_vault()
        (v / "Knowledge" / "Vault Health 2026-08-01.md").write_text("---\ntype: vault-health-report\n---\n## Summary\n- **Issues:** 7\n")
        self.assertEqual(vj.prior_health(v, TODAY), (7, "2026-08-01"))

    def test_health_writes_note(self):
        v = tmp_vault(); calls = []
        line = vj.health(v, today=TODAY, runner=lambda argv: (calls.append(argv) or json.dumps(self.DATA)))
        self.assertEqual(line, "health: 10 notes, 2 issues (error 0, warning 1, info 0, critical 1)")
        self.assertTrue((v / "Knowledge" / "Vault Health 2026-09-06.md").exists())
        self.assertIn("--json", calls[0]); self.assertIn(str(v), calls[0])

    def test_summary_omits_empty_critical_and_other(self):
        issues = [{"severity": "error"}, {"severity": "warning"}, {"severity": "info"}, {"severity": "info"}]
        self.assertEqual(vj.health_summary(9, issues), "health: 9 notes, 4 issues (error 1, warning 1, info 2)")
        self.assertEqual(vj.health_summary(9, issues + [{"severity": "blocker"}]),
                         "health: 9 notes, 5 issues (error 1, warning 1, info 2, other 1)")

    def test_unknown_severity_goes_under_other(self):
        data = {"total_notes": 4, "issues": [{"severity": "blocker", "message": "Weird", "files": ["z.md"]}]}
        out = vj.render_health(data, TODAY, None)
        self.assertIn("## Other\n- Weird (1 files)\n", out)


class MachineCommitter(unittest.TestCase):
    """C3/M054's vault-side half (row 127): vault_jobs.health() runs the same
    committer rule as maintenance/tests/test_vault_git.py, but nightly and
    read-only, against a real (fixture, here) vault repo. Never blocks -
    the finding lands in the health note and the returned summary line."""

    def _git_vault(self):
        v = tmp_vault()
        subprocess.run(["git", "init", "-q", str(v)], check=True)
        subprocess.run(["git", "-C", str(v), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(v), "config", "user.name", "owner"], check=True)

        def commit(rel, subject):
            p = v / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(subject)
            subprocess.run(["git", "-C", str(v), "add", rel], check=True)
            subprocess.run(["git", "-C", str(v), "commit", "-q", "-m", subject], check=True)
        return v, commit

    def test_no_findings_on_a_clean_history(self):
        v, commit = self._git_vault()
        commit("_CLAUDE.md", "human edit: tidy")
        commit("Daily/x.md", "maint(nightly): 2026-09-06")
        self.assertEqual(vj.machine_committer_findings(v), [])

    def test_a_planted_maint_commit_touching_claude_md_is_caught(self):
        v, commit = self._git_vault()
        commit("_CLAUDE.md", "maint(inbox): 2026-09-06")
        got = vj.machine_committer_findings(v)
        self.assertEqual(len(got), 1, got)
        self.assertIn("_CLAUDE.md", got[0])
        self.assertIn("maint(inbox)", got[0])

    def test_a_planted_maint_commit_touching_references_is_caught(self):
        v, commit = self._git_vault()
        commit("references/ai-first-rules.md", "maint(weekly): 2026-09-06")
        got = vj.machine_committer_findings(v)
        self.assertEqual(len(got), 1, got)
        self.assertIn("references/", got[0])

    def test_health_writes_the_finding_into_the_note_and_summary(self):
        v, commit = self._git_vault()
        commit("_CLAUDE.md", "maint(inbox): 2026-09-06")
        data = {"total_notes": 1, "issues": []}
        line = vj.health(v, today=TODAY, runner=lambda argv: json.dumps(data))
        self.assertIn("machine-committer: 1 finding(s)", line)
        note = (v / "Knowledge" / "Vault Health 2026-09-06.md").read_text()
        self.assertIn("## Machine committer (C3/M054)", note)
        self.assertIn("_CLAUDE.md", note)

    def test_health_note_carries_no_section_when_clean(self):
        v, commit = self._git_vault()
        commit("Daily/x.md", "maint(nightly): 2026-09-06")
        data = {"total_notes": 1, "issues": []}
        line = vj.health(v, today=TODAY, runner=lambda argv: json.dumps(data))
        self.assertNotIn("machine-committer", line)
        note = (v / "Knowledge" / "Vault Health 2026-09-06.md").read_text()
        self.assertNotIn("Machine committer", note)


class HealthRealShape(unittest.TestCase):
    """C1/I3: the real checker emits error/warning/info, never 'critical'."""
    FIXTURE = Path(__file__).resolve().parent / "fixtures" / "vault_health_sample.json"

    def setUp(self):
        self.data = json.loads(self.FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(sorted({i["severity"] for i in self.data["issues"]}), ["error", "info", "warning"])

    def test_error_rows_are_rendered_and_counted(self):
        issues = vj.filter_copilot(self.data["issues"])
        out = vj.render_health({**self.data, "issues": issues}, TODAY, None)
        self.assertIn("## Error\n- Unfilled template syntax in: Architecture/capsule-corp/2026-09-06-vault-jobs-design.md (1 files)\n", out)
        self.assertNotIn("## Critical", out)
        self.assertLess(out.index("## Error"), out.index("## Warning"))
        self.assertLess(out.index("## Warning"), out.index("## Info"))
        line = vj.health_summary(self.data["total_notes"], issues)
        self.assertEqual(line, "health: 127 notes, 4 issues (error 2, warning 1, info 1)")
        self.assertNotIn("critical", line)

    def test_every_message_rendered_and_counts_sum_to_total(self):
        issues = vj.filter_copilot(self.data["issues"])
        out = vj.render_health({**self.data, "issues": issues}, TODAY, None)
        for i in issues:
            self.assertIn(i["message"], out)
        counts = vj.severity_counts(issues)
        self.assertEqual(sum(counts.values()), len(issues))
        self.assertIn(f"total_issues: {len(issues)}\n", out)
        rendered = sum(1 for l in out.splitlines() if l.startswith("- ") and l.endswith("files)"))
        self.assertEqual(rendered, len(issues))


def _git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True, check=True).stdout.strip()

def git_vault():
    v = tmp_vault()
    subprocess.run(["git", "init", "-q", "-b", "main", str(v)], check=True, capture_output=True)
    _git(v, "config", "user.email", "t@t"); _git(v, "config", "user.name", "t")
    return v

TEMPLATE = '---\ndate: <% tp.date.now("YYYY-MM-DD") %>\ntype: daily\n---\n\n# <% tp.date.now("YYYY-MM-DD") %> - <% tp.date.now("dddd") %>\n\n## 🌅 Morning\n\n**Intention:** <% tp.file.cursor() %>\n\n---\n\n## 💼 Work Log\n\n\n\n---\n\n## 🏠 Personal\n\n'


class Morning(unittest.TestCase):
    def test_template(self):
        out = vj.render_template(TEMPLATE, TODAY)
        self.assertIn("date: 2026-09-06\n", out); self.assertIn("# 2026-09-06 - Sunday", out)
        self.assertIn("**Intention:** \n", out); self.assertNotIn("<%", out)

    def test_stale_projects(self):
        v = git_vault()
        (v / "Projects" / "Fresh.md").write_text("---\nstatus: active\nupdated: 2026-09-05\n---\n")
        (v / "Projects" / "Old.md").write_text("---\nstatus: active\nupdated: 2026-08-20\n---\n")
        (v / "Projects" / "Git.md").write_text("---\nstatus: active\n---\n")
        (v / "Projects" / "Paused.md").write_text("---\nstatus: paused\nupdated: 2026-01-01\n---\n")
        _git(v, "add", "."); subprocess.run(["git", "-C", str(v), "commit", "-qm", "c", "--date", "2026-08-01T00:00:00"], check=True, env={**os.environ, "GIT_COMMITTER_DATE": "2026-08-01T00:00:00"})
        self.assertEqual(vj.stale_projects(v, TODAY), [("Git", dt.date(2026, 8, 1)), ("Old", dt.date(2026, 8, 20))])

    def test_stale_projects_walks_subfolders(self):
        v = git_vault()
        (v / "Projects" / "sample-app").mkdir()
        (v / "Projects" / "Top.md").write_text("---\nstatus: active\nupdated: 2026-08-20\n---\n")
        proj = v / "Projects" / "sample-app"
        (proj / "Architecture - Overview.md").write_text(
            "---\nstatus: active\nupdated: 2026-08-10\n---\n")
        (proj / "Fresh.md").write_text("---\nstatus: active\nupdated: 2026-09-05\n---\n")
        self.assertEqual(vj.stale_projects(v, TODAY),
                         [("Top", dt.date(2026, 8, 20)),
                          ("sample-app/Architecture - Overview", dt.date(2026, 8, 10))])

    def test_sections_and_upsert(self):
        sec = vj.morning_sections([])
        self.assertEqual(sec, "## ⚠️ Stale projects (7+ days idle)\n\nNone.\n")
        note = vj.render_template(TEMPLATE, TODAY)
        once = vj.upsert_morning(note, sec)
        wl = once.index(vj.WORKLOG_HEAD)
        hr = once.index("\n---", wl)
        self.assertTrue(wl < once.index(vj.STALE_HEAD) < hr)
        personal_note = note.index("## 🏠 Personal")
        personal_once = once.index("## 🏠 Personal")
        self.assertEqual(once[:wl], note[:wl])
        self.assertEqual(once[personal_once:], note[personal_note:])
        twice = vj.upsert_morning(once, vj.morning_sections([("P", dt.date(2026, 8, 1))]))
        self.assertEqual(twice.count(vj.STALE_HEAD), 1)
        self.assertIn("- [[Projects/P]] - last activity 2026-08-01", twice)
        self.assertNotIn("None.", twice)

    def test_last_activity_git_fallback_on_bad_updated(self):
        v = git_vault()
        p = v / "Projects" / "Bad.md"
        p.write_text("---\nstatus: active\nupdated: not-a-date\n---\n")
        _git(v, "add", "."); subprocess.run(["git", "-C", str(v), "commit", "-qm", "c", "--date", "2026-08-15T00:00:00"], check=True, env={**os.environ, "GIT_COMMITTER_DATE": "2026-08-15T00:00:00"})
        self.assertEqual(vj.last_activity(v, p), dt.date(2026, 8, 15))

    def test_upsert_preserves_text_after_the_stale_block(self):
        """C2: the real Daily note ends with the pair; hand-written text after it must survive."""
        note = ("---\ndate: 2026-09-06\n---\n\n## \U0001f4bc Work Log\n\n- did a thing\n\n"
                + vj.morning_sections([("P", dt.date(2026, 8, 1))])
                + "\nEvening: shipped X\n- [ ] follow up tomorrow\n")
        out = vj.upsert_morning(note, vj.morning_sections([]))
        self.assertIn("Evening: shipped X\n- [ ] follow up tomorrow\n", out)
        self.assertEqual(out.count(vj.STALE_HEAD), 1)
        self.assertIn("None.", out); self.assertNotIn("- [[Projects/P]]", out)
        self.assertIn("- did a thing", out)
        self.assertEqual(vj.upsert_morning(out, vj.morning_sections([])), out)   # idempotent

    def test_upsert_keeps_a_handwritten_bullet_list_under_the_block(self):
        """Re-review: only the rendered row shape is the block's own row."""
        note = ("## \U0001f4bc Work Log\n\n- did a thing\n\n"
                + vj.morning_sections([])          # ends "...idle)\n\nNone.\n"
                + "\n- [ ] my own todo\n- [ ] a second todo\n")
        out = vj.upsert_morning(note, vj.morning_sections([("P", dt.date(2026, 8, 1))]))
        self.assertIn("- [ ] my own todo\n- [ ] a second todo\n", out)
        self.assertEqual(out.count(vj.STALE_HEAD), 1)
        self.assertIn("- [[Projects/P]] - last activity 2026-08-01", out)
        again = vj.upsert_morning(out, vj.morning_sections([("P", dt.date(2026, 8, 1))]))
        self.assertEqual(again, out)                    # idempotent, todos still there
        self.assertIn("- [ ] my own todo\n- [ ] a second todo\n", again)

    def test_own_line_only_matches_rendered_rows(self):
        self.assertTrue(vj._own_line(""))
        self.assertTrue(vj._own_line("None."))
        self.assertTrue(vj._own_line(
            "- [[Projects/sample-app/Overview]] - last activity 2026-08-10"))
        self.assertFalse(vj._own_line("- [ ] my own todo"))
        self.assertFalse(vj._own_line("- [ ] Late (due 2026-09-01)"))
        self.assertFalse(vj._own_line("- a plain bullet"))
        self.assertFalse(vj._own_line("- [[Projects/X]] - last activity soon"))
        self.assertFalse(vj._own_line("Evening: shipped X"))
        self.assertFalse(vj._own_line("---"))

    def test_upsert_replaces_block_at_end_of_file(self):
        note = ("## \U0001f4bc Work Log\n\n- x\n\n"
                + vj.morning_sections([("Old", dt.date(2026, 9, 1))]))
        out = vj.upsert_morning(note, vj.morning_sections([]))
        self.assertEqual(out.count(vj.STALE_HEAD), 1); self.assertNotIn("- [[Projects/Old]]", out)
        self.assertTrue(out.endswith("None.\n"))

    def test_upsert_morning_appends_when_no_worklog(self):
        note = "# Some note\n\nbody text\n"
        sec = vj.morning_sections([])
        out = vj.upsert_morning(note, sec)
        self.assertEqual(out.count(vj.STALE_HEAD), 1)
        self.assertEqual(out, note.rstrip("\n") + "\n\n" + sec)

    def test_morning_creates_then_updates(self):
        v = git_vault()
        (v / "Templates" / "Daily Note.md").write_text(TEMPLATE)
        self.assertTrue(vj.morning(v, TODAY).startswith(
            "morning: created Daily/2026-09-06.md stale=0"))
        txt = (v / "Daily" / "2026-09-06.md").read_text()
        self.assertIn(vj.STALE_HEAD, txt)
        self.assertNotIn("Due today", txt)
        self.assertTrue(vj.morning(v, TODAY).startswith("morning: updated Daily/2026-09-06.md"))
        self.assertEqual((v / "Daily" / "2026-09-06.md").read_text().count(vj.STALE_HEAD), 1)

    def test_the_first_post_2c_run_deletes_a_pre_2c_due_block(self):
        """The last pre-2c morning run left `## 📋 Due today` and its rows in
        today's note. Splicing from the legacy heading is what removes them
        (ruling 12), instead of orphaning the block above the stale one."""
        v = git_vault()
        (v / "Templates" / "Daily Note.md").write_text(TEMPLATE)
        note = v / "Daily" / f"{TODAY.isoformat()}.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text("## 💼 Work Log\n\n- did a thing\n\n"
                        "## 📋 Due today\n\n- [ ] Late (due 2026-09-01)\n\n"
                        + vj.morning_sections([]) + "\nEvening: shipped X\n", encoding="utf-8")
        vj.morning(v, TODAY)
        txt = note.read_text(encoding="utf-8")
        self.assertNotIn("Due today", txt)
        self.assertNotIn("Late (due", txt)
        self.assertEqual(txt.count(vj.STALE_HEAD), 1)
        self.assertIn("- did a thing", txt)
        self.assertIn("Evening: shipped X", txt)

    def test_morning_missing_template_exits_1(self):
        v = git_vault()
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            vj.morning(v, TODAY)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("morning: template missing:", err.getvalue())
        self.assertFalse((v / "Daily" / "2026-09-06.md").exists())


BOARD_NOW = "## 📥 Backlog\n\n- [ ] **B1**\n- [ ] **B2**\n\n## 🔬 Spec Ready\n\n- [ ] **S1** [[Specs/s1]]\n\n## ✅ Approved\n\n## 🏃 Sprint\n\n- [ ] **Q1**\n\n## 🔨 In Progress\n\n- [ ] **P1**\n\n## 👀 Review\n\n## ✅ Done\n\n- [x] ~~**D-new**~~\n- [x] ~~**D-old**~~\n"
BOARD_PREV = "## 📥 Backlog\n\n- [ ] **D-new**\n\n## ✅ Done\n\n- [x] ~~**D-old**~~\n"


class Digest(unittest.TestCase):
    def test_standup_lines(self):
        lines = vj.standup(BOARD_NOW, BOARD_PREV, TODAY)
        self.assertEqual(lines, ["  In Progress: P1", "  Moved to Done since yesterday: D-new",
                                 "  Spec Ready (awaiting approval): 1: S1",
                                 "  Sprint / In Progress / Review: 1 / 1 / 0",
                                 "  Sprint queue: Q1", "  Backlog depth: 2"])

    def test_the_standup_has_no_due_line(self):
        for l in vj.standup(BOARD_NOW, BOARD_PREV, TODAY):
            self.assertNotIn("due", l.lower())

    def test_standup_empty(self):
        lines = vj.standup("## ✅ Done\n", None, TODAY)
        self.assertEqual(lines[0], "  In Progress: clear"); self.assertEqual(lines[1], "  Moved to Done since yesterday: none")
        self.assertEqual(lines[4], "  Sprint queue: empty"); self.assertEqual(lines[5], "  Backlog depth: 0")

    def test_standup_empty_prev_board_means_all_moved(self):
        board_now = "## ✅ Done\n\n- [x] ~~**D1**~~\n- [x] ~~**D2**~~\n"
        lines = vj.standup(board_now, "", TODAY)
        self.assertEqual(lines[1], "  Moved to Done since yesterday: D1, D2")

    def test_join_truncates_at_max_items(self):
        board_now = "## 🔨 In Progress\n\n" + "\n".join(f"- [ ] **{c}**" for c in "abcdefgh") + "\n"
        lines = vj.standup(board_now, None, TODAY)
        self.assertEqual(lines[0], "  In Progress: a, b, c, d, e, f +2 more")

    def test_git_parts(self):
        v = git_vault()
        (v / "Boards" / "Engineering.md").write_text(BOARD_PREV); _git(v, "add", "."); _git(v, "commit", "-qm", "maint(nightly): 2026-09-05T230003")
        sha, ts = vj.nightly_commit(v)
        self.assertEqual(vj.board_at(v, sha), BOARD_PREV)
        (v / "Boards" / "Engineering.md").write_text(BOARD_NOW); (v / "Knowledge" / "n.md").write_text("x"); _git(v, "add", "."); _git(v, "commit", "-qm", "maint(nightly): 2026-09-06T230003")
        sha2, _ = vj.nightly_commit(v)
        self.assertNotEqual(sha, sha2); self.assertEqual(sorted(vj.changed_files(v, sha2)), ["Boards/Engineering.md", "Knowledge/n.md"])
        self.assertIsNone(vj.nightly_commit(tmp_vault()))

    def test_resurface_and_conflicts_and_retrieval(self):
        v = tmp_vault()
        (v / "Ideas" / "Old idea.md").write_text("---\ntype: idea\nupdated: 2026-07-01\n---\n# Old idea\n")
        (v / "Ideas" / "Older.md").write_text("---\ntype: speculation\nupdated: 2026-06-01\n---\n")
        (v / "Ideas" / "New.md").write_text("---\ntype: idea\nupdated: 2026-09-01\n---\n")
        (v / "Inbox" / "Clash.md").write_text("---\ntype: conflict\n---\n# Clash\n")
        (v / "Knowledge" / "Retrieval Eval 2026-09.md").write_text("---\ntype: retrieval-eval\nrecall-at-5: 0.759\nmrr: 0.573\n---\n")
        self.assertEqual(vj.resurface(v, TODAY, []), ("Older", "Ideas/Older", "2026-06-01"))
        self.assertEqual(vj.resurface(v, TODAY, ["Ideas/Older"]), ("Old idea", "Ideas/Old idea", "2026-07-01"))
        self.assertIsNone(vj.resurface(v, TODAY, ["Ideas/Older", "Ideas/Old idea"]))
        self.assertEqual(vj.conflicts(v), [("Clash", "Inbox/Clash")])
        self.assertEqual(vj.retrieval_line(v, TODAY), "  Retrieval: recall@5 76%, MRR 0.57")
        self.assertIsNone(vj.retrieval_line(v, dt.date(2026, 10, 1)))

    def test_digest_full_shape(self):
        v = git_vault()
        (v / "Boards" / "Engineering.md").write_text(BOARD_PREV); _git(v, "add", "."); _git(v, "commit", "-qm", "maint(nightly): x")
        (v / "Boards" / "Engineering.md").write_text(BOARD_NOW)
        out = vj.digest(v, TODAY, exclude=[], now=dt.datetime.now())
        self.assertTrue(out.startswith("BOARD STANDUP\n  In Progress: P1\n"))
        self.assertIn("\n\nVAULT\n  Overnight: nightly changed 1 notes: Boards/Engineering.md\n  Needs you: nothing\n", out)
        self.assertNotIn("Resurfacing:", out); self.assertNotIn("Retrieval:", out)
        self.assertLess(len(out.splitlines()), 30)

    def test_overnight_counts_only_md_files(self):
        v = git_vault()
        (v / "Boards" / "Engineering.md").write_text(BOARD_PREV); _git(v, "add", "."); _git(v, "commit", "-qm", "maint(nightly): x")
        (v / "Boards" / "Engineering.md").write_text(BOARD_NOW); (v / ".obsidian-semantic-index.json").write_text("{}")
        _git(v, "add", "."); _git(v, "commit", "-qm", "maint(nightly): y")
        out = vj.digest(v, TODAY, exclude=[], now=dt.datetime.now())
        self.assertIn("  Overnight: nightly changed 1 notes: Boards/Engineering.md\n", out)

    def test_backlog_depth_counts_only_unticked(self):
        board = "## \U0001f4e5 Backlog\n\n- [ ] **B1**\n- [x] ~~**B2 done in place**~~\n- [ ] **B3**\n"
        self.assertEqual(vj.standup(board, None, TODAY)[5], "  Backlog depth: 2")

    def test_overnight_no_run_when_old(self):
        v = git_vault()
        (v / "Boards" / "Engineering.md").write_text(BOARD_PREV); _git(v, "add", "."); _git(v, "commit", "-qm", "maint(nightly): x")
        out = vj.digest(v, TODAY, exclude=[], now=dt.datetime.now() + dt.timedelta(days=3))
        self.assertIn("  Overnight: no run\n", out)


class Excludes(unittest.TestCase):
    """I1: vault paths contain commas, so the exclude list is read from a file."""
    COMMA_PATH = "Inbox/Mirror every repo's architecture, decisions and skills into the notes vault"

    def _vault(self):
        v = tmp_vault()
        (v / "Ideas" / "Keep.md").write_text("---\ntype: idea\nupdated: 2026-07-01\n---\n")
        (v / f"{self.COMMA_PATH}.md").write_text(
            "---\ntype: idea\nupdated: 2026-06-01\n---\n")
        return v

    def test_exclude_file_handles_comma_in_path(self):
        v = self._vault()
        self.assertEqual(vj.resurface(v, TODAY, [])[1], self.COMMA_PATH)
        f = v / "history.txt"
        f.write_text(self.COMMA_PATH + "\n\n", encoding="utf-8")
        self.assertEqual(vj.read_excludes("", f), [self.COMMA_PATH])
        self.assertEqual(vj.resurface(v, TODAY, vj.read_excludes("", f))[1], "Ideas/Keep")

    def test_read_excludes_merges_and_drops_placeholder(self):
        self.assertEqual(vj.read_excludes("a, b, (none yet), a"), ["a", "b"])
        self.assertEqual(vj.read_excludes("", "/nonexistent/history.txt"), [])

    def test_main_digest_wires_exclude_file(self):
        v = self._vault()
        (v / "Boards" / "Engineering.md").write_text(BOARD_NOW)
        f = v / "history.txt"; f.write_text(self.COMMA_PATH + "\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            vj.main(["digest", "--vault", str(v), "--exclude-file", str(f)])
        out = buf.getvalue()
        self.assertIn("BOARD STANDUP", out)
        self.assertIn("Resurfacing: Keep ([[Ideas/Keep]])", out)
        self.assertNotIn(self.COMMA_PATH, out)


if __name__ == "__main__":
    unittest.main()
