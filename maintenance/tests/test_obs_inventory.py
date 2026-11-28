import contextlib
import datetime
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("AGENT_OPS_NO_SEND", "1")
import obs_inventory  # noqa: E402

NOW = datetime.datetime(2026, 9, 8, 3, 0, 0, tzinfo=datetime.timezone.utc)

WORKER_PY = '''"""fixture worker."""
import events


def do():
    events.log("job", "worker", "did stuff")
    events.log("card", "worker", "hi", payload={"event": "x"})
'''

EVENTS_PY = '''"""fixture events."""
KINDS = {
    "job": (),
    "card": ("event",),
}
'''

OBS_PROBE_PY = '''"""fixture obs_probe."""
NAMES = {
    "capsule.probe.tick": {"collector": "probe", "labels": (), "unit": "1"},
    # singular names for the plural component rows "kanban boards" / "launchd
    # jobs" - the token stemming (I7) is what connects the two
    "capsule.board.cards": {"collector": "boards", "labels": ("board",), "unit": "1"},
    "capsule.job.age_s": {"collector": "launchd", "labels": ("job",), "unit": "s"},
}
'''

HEAL_TABLE_PY = '''"""fixture heal_table."""
ORDER = ["docker", "outbox"]
'''

CLEANUP_SH = '''#!/bin/sh
# delete events older than 30 days
docker exec capsule-corp-postgres psql -U ops -d ops -qc "delete from events where ts < now() - interval '30 days'"
# delete samples older than 30 days
docker exec capsule-corp-postgres psql -U ops -d ops -qc "delete from samples where ts < now() - interval '30 days'"
'''

COMPOSE_YML = '''name: capsule-corp
services:
  postgres:
    image: postgres:16-alpine  # v16.4
    container_name: capsule-corp-postgres
    restart: unless-stopped
    ports: ["127.0.0.1:5432:5432"]
    volumes:
      - ${DATA_DIR}/postgres:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ops -d ops"]
      interval: 10s
      timeout: 3s
      retries: 10
'''

FLUENT_BIT_CONF = '''[SERVICE]
    Flush        5

[INPUT]
    Name             tail
    Path             /vault-logs/*.log
    Tag              vault.*
    Path_Key         path
'''

LOKI_YAML = '''limits_config:
  retention_period: 168h
'''

BUILD_DASHBOARDS_PY = '''"""fixture build_dashboards."""

def build():
    return {
        "panels": [
            {"title": "Jobs panel",
             "targets": [{"rawSql": "select 1 from events where kind='job'"}]},
            {"title": "Ghost panel",
             "targets": [{"rawSql": "select 1 from samples where name='capsule.ghost'"}]},
        ]
    }


DASHBOARDS = {"test.json": build}
'''

RULES_YAML = '''apiVersion: 1
groups:
  - orgId: 1
    name: capsule-corp
    folder: Capsule Corp
    interval: 1m
    rules:
      - uid: job-failed
        title: Job failed
        condition: B
        data:
          - refId: A
            model:
              rawSql: "select count(*) from events where kind='job'"
'''

RUNBOOK_MD = '''## Postgres

Some content.

## Heal

Heal content.

## Observability

### launchd jobs

A `###` subsection, not a `## ` heading - the runbook column must see it too.
'''

CONVENTIONS_MD = '''---
contract_version: 1
---

# fixture conventions
'''

PLIST = '''<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>dev.capsule.capsule-corp-obs-inventory</string>
</dict>
</plist>
'''

PREV_NOTE = '''---
type: architecture
generated_by: obs_inventory.py
generated_at: 2026-01-01T00:00:00Z
contract_version: 1
ai-first: true
---

## For future agent

old preamble.

## Components (as of 2026-01-01)

| container | image | version | port | data dir | retention |
|---|---|---|---|---|---|
| capsule-corp-postgres | postgres:16-alpine | 16-alpine | 5432 | postgres | events 30d (cleanup.sh) |

## Signals

| kind/name | type | emitter (file:line) | store | retention | consuming panel(s) |
|---|---|---|---|---|---|
| old-kind | event | maintenance/old.py:1 | Postgres (events) | 30d (cleanup.sh) | — (orphan, see below) |

## Coverage matrix

| component | health | structured logs | metrics | traces | dashboard | alerts | SLO | runbook |
|---|---|---|---|---|---|---|---|---|

## Orphans

**Panels/alerts referencing a signal nobody emits:**
- none found

**Emitted signals no panel or alert reads:**
- 'old-kind' (event) is emitted but no dashboard panel or alert rule reads it

## Diff since last run

first run - no previous note to diff against.
'''


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _build_fixture_repo(root):
    _write(root / "maintenance" / "worker.py", WORKER_PY)
    _write(root / "maintenance" / "events.py", EVENTS_PY)
    _write(root / "maintenance" / "obs_probe.py", OBS_PROBE_PY)
    _write(root / "maintenance" / "heal_table.py", HEAL_TABLE_PY)
    _write(root / "maintenance" / "cleanup.sh", CLEANUP_SH)
    _write(root / "observability" / "docker-compose.yml", COMPOSE_YML)
    _write(root / "observability" / "fluent-bit" / "fluent-bit.conf", FLUENT_BIT_CONF)
    _write(root / "observability" / "loki" / "loki.yaml", LOKI_YAML)
    _write(root / "observability" / "grafana" / "build_dashboards.py", BUILD_DASHBOARDS_PY)
    _write(root / "observability" / "grafana" / "provisioning" / "alerting" / "rules.yaml", RULES_YAML)
    _write(root / "docs" / "RUNBOOK.md", RUNBOOK_MD)
    _write(root / "docs" / "observability-conventions.md", CONVENTIONS_MD)
    _write(root / "launchd" / "templates" / "__PREFIX__.capsule-corp-obs-inventory.plist", PLIST)


class Render(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.vault = Path(self.tmp.name) / "vault"
        _build_fixture_repo(self.repo)
        self.vault.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_six_sections_present(self):
        text = obs_inventory.render(self.repo, self.vault, NOW)
        self.assertIn("## Components", text)
        self.assertIn("as of 2026-09-08", text)
        self.assertIn("## Signals", text)
        self.assertIn("## Coverage matrix", text)
        self.assertIn("## Orphans", text)
        self.assertIn("## Diff since last run", text)
        self.assertIn("## For future agent", text)

    def test_frontmatter(self):
        text = obs_inventory.render(self.repo, self.vault, NOW)
        self.assertIn("type: architecture", text)
        self.assertIn("generated_by: obs_inventory.py", text)
        self.assertIn("generated_at: 2026-09-08T03:00:00Z", text)
        self.assertIn("contract_version: 1", text)
        self.assertIn("ai-first: true", text)

    def test_emitter_file_line_recorded(self):
        text = obs_inventory.render(self.repo, self.vault, NOW)
        self.assertIn("worker.py:6", text)  # events.log("job", ...) call site

    def test_consuming_panel_listed(self):
        text = obs_inventory.render(self.repo, self.vault, NOW)
        self.assertIn("Jobs panel", text)

    def test_orphan_consumer_reference_to_unemitted_name(self):
        text = obs_inventory.render(self.repo, self.vault, NOW)
        self.assertIn("capsule.ghost", text)
        self.assertIn("nothing emits it", text)

    def test_orphan_emitted_signal_no_panel_reads(self):
        text = obs_inventory.render(self.repo, self.vault, NOW)
        self.assertIn("'card' (event) is emitted but no dashboard panel or alert rule reads it", text)

    def test_coverage_matrix_twelve_rows_eight_columns(self):
        text = obs_inventory.render(self.repo, self.vault, NOW)
        for row in obs_inventory.component_rows():
            self.assertIn(f"| {row} |", text)
        for col in obs_inventory.MATRIX_COLUMNS:
            self.assertIn(col, text)
        # other-app has nothing in this fixture at all - every cell falsy
        self.assertIn("other-app", text)
        self.assertEqual(len(obs_inventory.component_rows()), 12)
        self.assertEqual(len(obs_inventory.MATRIX_COLUMNS), 8)

    def test_component_rows_default_has_no_project_rows(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"CAPSULE_HOME": home}):
            self.assertEqual(obs_inventory.component_rows(),
                             list(obs_inventory.BASE_COMPONENT_ROWS))
        self.assertEqual(obs_inventory.component_rows()[-2:], ["sample-app", "other-app"])

    def test_dry_run_writes_nothing(self):
        # --dry-run prints the whole note; keep it in the test, not the suite log.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = obs_inventory.main(["--repo", str(self.repo), "--vault", str(self.vault),
                                      "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("## Signals", buf.getvalue())
        self.assertFalse((self.vault / obs_inventory.NOTE_REL).exists())

    def test_diff_section_shows_change_against_previous_note(self):
        note_path = self.vault / obs_inventory.NOTE_REL
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(PREV_NOTE, encoding="utf-8")
        text = obs_inventory.render(self.repo, self.vault, NOW)
        diff = obs_inventory._section(text, "Diff since last run")
        self.assertNotIn("first run", diff)
        self.assertIn("old-kind", diff)

    def test_first_run_diff_section(self):
        text = obs_inventory.render(self.repo, self.vault, NOW)
        diff = obs_inventory._section(text, "Diff since last run")
        self.assertIn("first run", diff)

    def _matrix_cell(self, text, row, column):
        """The one cell of the rendered coverage matrix at (row, column)."""
        header = "| component | " + " | ".join(obs_inventory.MATRIX_COLUMNS) + " |"
        self.assertIn(header, text)
        line = next(l for l in text.splitlines() if l.startswith(f"| {row} |"))
        cells = [c.strip() for c in line.strip("|").split("|")]
        return cells[1 + obs_inventory.MATRIX_COLUMNS.index(column)]

    def test_samples_retention_reads_cleanup_sh(self):
        """I7a: `samples` has a retention rule (cleanup.sh) - the note used to
        say 'n/a (no cleanup rule found)' for every sample row."""
        self.assertEqual(obs_inventory._samples_retention(self.repo), "30d (cleanup.sh)")
        text = obs_inventory.render(self.repo, self.vault, NOW)
        self.assertIn("30d (cleanup.sh)", text)
        self.assertNotIn("no cleanup rule found", text)

    def test_plural_component_rows_see_singular_sample_names(self):
        """I7b: `capsule.board.*` must light up the `kanban boards` row and
        `capsule.job.*` the `launchd jobs` row."""
        text = obs_inventory.render(self.repo, self.vault, NOW)
        self.assertEqual(self._matrix_cell(text, "kanban boards", "metrics"), "✅")
        self.assertEqual(self._matrix_cell(text, "launchd jobs", "metrics"), "✅")
        self.assertEqual(self._matrix_cell(text, "sample-app", "metrics"), "❌",
                          "a row with no signals at all must stay ❌")

    def test_runbook_column_sees_hash_hash_hash_headings(self):
        """I7c: the fixture runbook names `launchd jobs` in a `###`
        subsection, not a `## ` heading."""
        self.assertIn("launchd jobs", obs_inventory._runbook_headings(self.repo))
        text = obs_inventory.render(self.repo, self.vault, NOW)
        self.assertEqual(self._matrix_cell(text, "launchd jobs", "runbook"), "✅")
        self.assertEqual(self._matrix_cell(text, "postgres", "runbook"), "✅")  # `## Postgres`
        self.assertEqual(self._matrix_cell(text, "other-app", "runbook"), "❌")

    def test_image_cell_drops_the_compose_pin_comment(self):
        """I7: `image: postgres:16-alpine  # v16.4` - the comment feeds the
        version column, it does not belong in the image cell."""
        text = obs_inventory.render(self.repo, self.vault, NOW)
        line = next(l for l in text.splitlines() if l.startswith("| capsule-corp-postgres |"))
        cells = [c.strip() for c in line.strip("|").split("|")]
        self.assertEqual(cells[1], "postgres:16-alpine")
        self.assertEqual(cells[2], "16.4")

    def test_repo_unreadable_exits_two(self):
        rc = obs_inventory.main(["--repo", "/nonexistent-capsule-corp-repo-xyz",
                                  "--vault", str(self.vault), "--dry-run"])
        self.assertEqual(rc, 2)


class MainPush(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.vault = Path(self.tmp.name) / "vault"
        _build_fixture_repo(self.repo)
        self.vault.mkdir(parents=True, exist_ok=True)
        # host.json (scratch CAPSULE_HOME) names the machine's "real" vault: never touched
        self.real = Path(self.tmp.name) / "configured-vault"
        self.real.mkdir()
        (Path(self.tmp.name) / "capsule-home").mkdir()
        (Path(self.tmp.name) / "capsule-home" / "host.json").write_text(
            json.dumps({"vault": {"path": str(self.real)}}))
        self._orig_home = os.environ.get("CAPSULE_HOME")
        os.environ["CAPSULE_HOME"] = str(Path(self.tmp.name) / "capsule-home")
        self.calls = {"pull": [], "push": []}
        self._orig_pull = obs_inventory.vault_git.pull
        self._orig_push = obs_inventory.vault_git.push

        def fake_pull(vault, runner=None):
            self.calls["pull"].append(vault)
            return True

        def fake_push(vault, paths, message, runner=None):
            self.calls["push"].append((vault, list(paths), message))
            return True

        obs_inventory.vault_git.pull = fake_pull
        obs_inventory.vault_git.push = fake_push

    def tearDown(self):
        obs_inventory.vault_git.pull = self._orig_pull
        obs_inventory.vault_git.push = self._orig_push
        if self._orig_home is None:
            os.environ.pop("CAPSULE_HOME", None)
        else:
            os.environ["CAPSULE_HOME"] = self._orig_home
        self.tmp.cleanup()

    def test_pull_happens_before_the_note_is_written(self):
        """I6: pull, then render, then write, then push - so the `## Diff
        since last run` section diffs against the note as the remote has it."""
        order = []
        note_path = self.vault / obs_inventory.NOTE_REL

        def ordered_pull(vault, runner=None):
            order.append(("pull", note_path.exists()))
            return True

        def ordered_push(vault, paths, message, runner=None):
            order.append(("push", note_path.exists()))
            return True

        obs_inventory.vault_git.pull = ordered_pull
        obs_inventory.vault_git.push = ordered_push
        rc = obs_inventory.main(["--repo", str(self.repo), "--vault", str(self.vault)])
        self.assertEqual(rc, 0)
        self.assertEqual(order, [("pull", False), ("push", True)])

    def test_push_failure_is_exit_1(self):
        obs_inventory.vault_git.push = lambda *a, **k: False
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = obs_inventory.main(["--repo", str(self.repo), "--vault", str(self.vault)])
        self.assertEqual(rc, 1)
        self.assertIn("vault push failed", buf.getvalue())
        # the note is still on disk - deploy treats this step as warn-only
        self.assertTrue((self.vault / obs_inventory.NOTE_REL).exists())

    def test_refuses_the_default_vault_under_test(self):
        with self.assertRaises(RuntimeError):
            obs_inventory.main(["--repo", str(self.repo), "--vault", ""])
        with self.assertRaises(RuntimeError):
            obs_inventory.main(["--repo", str(self.repo),
                                 "--vault", str(self.real)])
        self.assertEqual(self.calls["push"], [], "nothing may be pushed on a refused run")

    def test_main_writes_note_and_pushes_correct_path(self):
        rc = obs_inventory.main(["--repo", str(self.repo), "--vault", str(self.vault)])
        self.assertEqual(rc, 0)
        note_path = self.vault / obs_inventory.NOTE_REL
        self.assertTrue(note_path.exists())
        self.assertEqual(len(self.calls["push"]), 1)
        vault_arg, paths_arg, message = self.calls["push"][0]
        self.assertEqual(Path(vault_arg).resolve(), self.vault.resolve())
        self.assertEqual(paths_arg, [obs_inventory.NOTE_REL])
        self.assertEqual(message, "observability: inventory")
        self.assertEqual(len(self.calls["pull"]), 1)




_OLD_HOME = os.environ.get("CAPSULE_HOME")


def setUpModule():
    """Never read the machine's real host.json: this suite's own host config."""
    home = tempfile.mkdtemp()
    Path(home, "host.json").write_text(
        '{"observability": {"components": ["sample-app", "other-app"]}}')
    os.environ["CAPSULE_HOME"] = home


def tearDownModule():
    if _OLD_HOME is None:
        os.environ.pop("CAPSULE_HOME", None)
    else:
        os.environ["CAPSULE_HOME"] = _OLD_HOME


if __name__ == "__main__":
    unittest.main()
