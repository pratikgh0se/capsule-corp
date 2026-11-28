# mutation-check: targets-predate-range b957739c9 -- rename only, no behaviour pinned ::
#   Tables.test_era_is_constrained_to_capsule_or_legacy
"""Task 1 (The Lookout): the 003 migration's static contract. Oracle 1 is this file's
literal copy of spec 5.3/5.4/5.5; oracle 2 is tests/test_migration_pg.sh applying the
same SQL to a real postgres:16-alpine (docker, run by the owner or CI)."""
import json
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "maintenance"))
from capsule import lookout  # noqa: E402
import ai_usage  # noqa: E402
import migration_lint  # noqa: E402
from capsule import legacy_names as _LN  # noqa: E402

MIG = REPO / "observability" / "postgres" / "migrations" / "003-lookout.sql"

TABLES = {
    "cap_cards": {"board", "task_id", "snap_ts", "pass_id", "title", "status", "product", "epic", "repo",
                  "phase", "role_now", "model_now", "run_id_now", "block_kind", "block_reason", "waiting_on",
                  "waiting_words", "waiting_since", "failure_count", "consecutive_failures", "risk_class",
                  "tier", "created_at", "started_at", "completed_at", "commits", "review_rounds"},
    "cap_runs": {"board", "run_id", "snap_ts", "pass_id", "task_id", "role", "model", "provider", "account",
                 "runner", "status", "outcome", "error_class", "attempts", "started_at", "ended_at",
                 "last_heartbeat_at", "tokens_in", "tokens_out", "tokens_cache_read", "tokens_cache_write", "era"},
    "cap_blocks": {"board", "task_id", "opened_at", "snap_ts", "pass_id", "kind", "reason", "waiting_on",
                   "opened_event_id", "closed_at", "closed_by"},
    "cap_epics": {"board", "epic", "snap_ts", "pass_id", "product", "repo", "spec", "decision_rows",
                  "cards_total", "cards_done", "cards_blocked", "head_sha", "ahead_of_main", "pr_number",
                  "pr_url", "publish_state", "publish_reason", "publish_state_since"},
    "cap_prs": {"repo", "pr_number", "snap_ts", "pass_id", "board", "epic", "url", "state", "checks",
                "review_decision", "head_sha", "cards_in_pr", "opened_at", "ready_at", "merged_at",
                "merge_sha", "closed_at", "updated_at"},
    "cap_commits": {"repo", "sha", "snap_ts", "pass_id", "board", "epic", "task_id", "run_id", "committed_at",
                    "validator", "reviewer", "guide", "review_rounds", "overrides", "pushed_at", "pr_number",
                    "pr_comment_url"},
    "cap_incidents": {"board", "id", "snap_ts", "pass_id", "task_id", "run_id", "kind", "status", "role",
                      "opened_at", "closed_at", "root_cause", "fix_card", "repeat_of"},
    "cap_tick": {"board", "snap_ts", "pass_id", "host", "pid", "harness_sha", "last_pass_at", "pass_ms",
                 "lookout_ms", "counts", "running", "ready", "blocked", "paused_accounts",
                 "spool_bytes_unshipped", "last_ship_ok_at"},
    "prices": {"model", "valid_from", "usd_in_per_mtok", "usd_out_per_mtok", "usd_cache_read_per_mtok",
               "usd_cache_write_per_mtok"},
    "cap_event_words": {"kind", "words", "actor"},
}
VIEWS = {"v_floor", "v_waiting_on_pratik", "v_run_cost", "v_card_story", "v_since", "v_factory"}

# spec 5.5.1 (board kinds), 5.5.2 (publisher), 5.5.4 (migration), the synthetic `archived`
WORDS = {
    "created": ("card created", "tick"), "claimed": ("agent picked it up", "agent"),
    "claim_rejected": ("pickup refused", "tick"), "claim_extended": ("lease extended", "agent"),
    "reclaim_deferred": ("reclaim deferred", "tick"), "reclaimed": ("taken back from a stalled agent", "tick"),
    "promoted": ("moved to ready", "tick"), "request_review": ("sent for review", "agent"),
    "review_started": ("review started", "agent"), "review_retry": ("review retried", "tick"),
    "completed": ("committed", "tick"), "blocked": ("blocked", "tick"), "unblocked": ("unblocked", "pratik"),
    "decision_requested": ("needs your answer", "agent"), "first_screen_hold": ("screen to approve", "tick"),
    "failure_reset": ("failures reset", "pratik"), "artefact": ("agent produced a file", "agent"),
    "invalid_document": ("agent report was malformed", "tick"), "crashed": ("agent crashed", "tick"),
    "timed_out": ("agent ran out of time", "tick"), "spawn_failed": ("agent could not start", "tick"),
    "stale_run": ("run went stale", "tick"), "rate_limited": ("paused by rate limit", "tick"),
    "tick_error": ("factory error on this card", "tick"), "contradiction": ("rules disagree", "guide"),
    "contradiction_loop": ("rules keep disagreeing", "guide"), "ruling_started": ("guide started a ruling", "guide"),
    "guide_ruling": ("guide ruled", "guide"), "ruling_failed": ("guide ruling failed", "guide"),
    "aligned_uncited": ("aligned without a citation", "guide"), "checks_overridden": ("checks overridden", "pratik"),
    "intervention": ("you stepped in", "pratik"), "accept_refused": ("acceptance refused", "tick"),
    "screen_refused": ("screen refused", "pratik"), "override": ("override applied", "pratik"),
    "override_refused": ("override refused", "tick"), "override_expired": ("override expired", "tick"),
    "contract_amended": ("contract amended", "pratik"), "checkpoint": ("checkpoint saved", "agent"),
    "checkpoint_restart": ("restarted from a checkpoint", "tick"),
    "incident_opened": ("Korin opened an incident", "korin"), "incident_closed": ("Korin closed an incident", "korin"),
    "fix_card_routed": ("fix card created", "korin"), "korin_started": ("Korin started", "korin"),
    "korin_applied": ("Korin applied a fix", "korin"), "korin_failed": ("Korin's fix failed", "korin"),
    "korin_gave_up": ("Korin gave up", "korin"),
    # kinds the live harness emits that spec 5.5.1's table missed (review I1)
    "parked": ("failed too often, parked", "tick"), "failed": ("run failed", "tick"),
    "dependency_wait": ("waiting for another card", "tick"),
    "block_loop_detected": ("block keeps coming back", "tick"),
    "first_screen_ok": ("screen approved", "pratik"), "first_screen_fix": ("screen sent back", "pratik"),
    "pushed": ("pushed to GitHub", "publisher"), "pr_opened": ("PR opened", "publisher"),
    "pr_comment": ("card posted to the PR", "publisher"), "pr_review_posted": ("branch review posted", "publisher"),
    "pr_ready": ("PR ready for your merge", "publisher"), "pr_checks": ("PR checks changed", "publisher"),
    "pr_merged": ("PR merged", "publisher"), "pr_closed": ("PR closed", "publisher"),
    "publish_refused": ("PR could not be published", "publisher"),
    "publish_ok": ("PR publishing recovered", "publisher"),
    "archived": ("archived", "tick"),
    "closed_stale": (f"closed as stale ({_LN.NAME.capitalize()} era)", "tick"),
}


def sql():
    return MIG.read_text(encoding="utf-8")


def table_columns(text, table):
    m = re.search(rf"create table if not exists {table} \((.*?)\);\n", text, re.S | re.I)
    if not m:
        return None
    cols = set()
    for line in m.group(1).splitlines():
        line = line.split("--")[0].strip()
        for part in line.split(","):
            word = part.strip().split(" ")[0] if part.strip() else ""
            if word and word.lower() not in ("primary", "check", "") and re.match(r"^[a-z_]+$", word):
                cols.add(word)
    return cols


class Tables(unittest.TestCase):
    def test_every_table_has_exactly_the_spec_columns(self):
        text = sql()
        for table, want in TABLES.items():
            with self.subTest(table=table):
                self.assertEqual(table_columns(text, table), want)

    def test_era_is_constrained_to_capsule_or_legacy(self):
        self.assertIn(f"era text not null check (era in ('capsule','{_LN.NAME}'))", sql())

    def test_primary_keys(self):
        text = sql()
        for pk in ("primary key (board, task_id))", "primary key (board, run_id))",
                   "primary key (board, task_id, opened_at))", "primary key (board, epic))",
                   "primary key (repo, pr_number))", "primary key (repo, sha))", "primary key (board, id))",
                   "board text primary key", "primary key (model, valid_from))"):
            self.assertIn(pk, text)

    def test_views_exist(self):
        found = set(re.findall(r"create or replace view (v_[a-z_]+)", sql()))
        self.assertEqual(found, VIEWS)

    def test_waiting_view_keeps_done_cards_that_wait_on_pratik(self):
        """Ruling 3 / review C3: a first-screen hold sits on a done card; only archived cards drop out."""
        branch_a = sql().split("create or replace view v_waiting_on_pratik as")[1].split("union all")[0]
        self.assertIn("where c.waiting_on = 'pratik' and c.phase <> 'archived'", branch_a)
        for verb in ('screen-ok', 'reset "<note>"', 'accept "<note>"', 'unblock "<answer>"'):
            self.assertIn(verb, branch_a)

    def test_owner_view_reads_owner_without_the_retired_board_filter_and_drops_the_old_view(self):
        from capsule.tests import private_names
        if not private_names.DENYLIST.is_file():
            self.skipTest(private_names.SKIP_MSG)
        text = (MIG.parent / "010-waiting-on-owner.sql").read_text(encoding="utf-8")
        init = (REPO / "observability" / "postgres" / "init.sql").read_text(encoding="utf-8")
        for body in (text, init):
            head = "create or replace view v_waiting_on_owner as"
            branch = body.split(head)[1].split("union all")[0]
            # the value the collector writes is the value the view reads
            want = f"where c.waiting_on = '{lookout.OWNER_WAIT}' and c.phase <> 'archived'"
            self.assertIn(want, branch)
            for name in private_names.project_names():
                self.assertNotIn(name, branch.lower())
        self.assertIn("drop view if exists v_waiting_on_pratik;", text)

    def test_since_view_reads_the_morning_brief_end_row_within_36h(self):
        text = sql()
        self.assertIn("payload->>'job' = 'morning'", text)
        self.assertIn("payload->>'phase' = 'end'", text)
        self.assertIn("interval '36 hours'", text)
        self.assertIn("'fallback_24h'", text)


class Seed(unittest.TestCase):
    def test_event_words_seed_equals_spec_table(self):
        rows = re.findall(r"\('([a-z_]+)', '((?:[^']|'')*)', '([a-z]+)'\)", sql())
        got = {k: (w.replace("''", "'"), a) for k, w, a in rows}
        self.assertEqual(got, WORDS)


class Lint(unittest.TestCase):
    def test_migration_lint_accepts_the_irreversible_header(self):
        self.assertEqual(migration_lint.lint(MIG.parent), [])
        self.assertRegex(sql(), r"(?m)^-- irreversible: row 478\b")

    def test_init_sql_carries_the_same_ddl(self):
        body = sql().split("-- BEGIN LOOKOUT DDL\n", 1)[1]
        self.assertIn(body, (REPO / "observability" / "postgres" / "init.sql").read_text(encoding="utf-8"))


class Prices(unittest.TestCase):
    def test_prices_json_is_the_only_price_source(self):
        doc = json.loads((REPO / "maintenance" / "prices.json").read_text())
        self.assertEqual(ai_usage.load_prices(), {m: dict(v) for m, v in doc["models"].items()})
        self.assertFalse(hasattr(ai_usage, "PRICES"), "the inline price table must be gone")
        self.assertEqual(ai_usage.FALLBACK_MODEL, doc["fallback"])

    def test_price_row_is_unchanged_for_every_model(self):
        # the numbers the inline table produced at 99c5f35 (intent: no price drift on the move)
        cases = {"claude-fable-5-1": 10 + 50 + 0.25 + 12.5,
                 "claude-opus-5": 5 + 25 + 0.5 + 6.25,
                 "claude-sonnet-5": 2 + 10 + 0.2 + 2.5,
                 "claude-haiku-4-5-20251001": 1 + 5 + 0.1 + 1.25}
        for model, usd_per_4m in cases.items():
            with self.subTest(model=model):
                row = ai_usage.price_row({"model": model, "input": 1_000_000, "output": 1_000_000,
                                          "cache_read": 1_000_000, "cache_write": 1_000_000})
                self.assertAlmostEqual(row["usd_estimate"], usd_per_4m, places=6)

    def test_explicit_cache_write_costs_the_same_in_pricing_and_in_v_run_cost(self):
        # v_run_cost (003-lookout.sql) sums tokens x rate, with
        # cache_write counted as coalesce(cache_write, 0).
        from capsule import lookout_ship
        shipped = {p["model"]: p for p in lookout_ship._prices()}
        for model in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5",
                      "claude-sonnet-5", "claude-fable-5-1", "claude-opus-5",
                      "claude-haiku-4-5-20251001"):
            with self.subTest(model=model):
                p = shipped[model]
                self.assertIsNotNone(p["cache_write"])
                sql = (1000 * p["in"] + 2000 * p["out"] + 30000 * p["cache_read"]
                       + 400000 * p["cache_write"]) / 1e6
                row = ai_usage.price_row({"model": model, "input": 1000, "output": 2000,
                                          "cache_read": 30000, "cache_write": 400000})
                self.assertAlmostEqual(row["usd_estimate"], sql, places=6)

    def test_a_price_edit_in_the_file_changes_the_estimate(self):
        import os
        import tempfile
        doc = json.loads((REPO / "maintenance" / "prices.json").read_text())
        doc["models"]["claude-opus-5"]["in"] = 7
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(doc, f)
        self.addCleanup(os.unlink, f.name)
        row = {"model": "claude-opus-5", "input": 1_000_000}
        self.assertAlmostEqual(ai_usage.price_row(dict(row), prices=ai_usage.load_prices(f.name))["usd_estimate"], 7)
        self.assertAlmostEqual(ai_usage.price_row(dict(row))["usd_estimate"], 5)


if __name__ == "__main__":
    unittest.main()
