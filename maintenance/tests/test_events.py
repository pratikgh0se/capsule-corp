import os
import csv, io, json, sys, tempfile, time, unittest, unittest.mock
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["AGENT_OPS_NO_SEND"] = "1"  # tests must never send Telegram messages
import tempfile as _tf
os.environ.setdefault("EVENTS_SPOOL", _tf.mkdtemp(prefix="events-test-") + "/events.jsonl")  # tests never write the live spool
import events

ROOT = Path(__file__).resolve().parents[2]


def _stub_docker_dir():
    """A temp dir holding an executable `docker` stub: what AGENT_OPS_DOCKER_BIN must point at."""
    d = Path(tempfile.mkdtemp())
    (d / "docker").write_text("#!/bin/sh\nexit 0\n")
    (d / "docker").chmod(0o755)
    return str(d)


class Log(unittest.TestCase):
    def setUp(self):
        self.spool = Path(tempfile.mkdtemp()) / "events.jsonl"

    def test_appends_one_json_line_with_required_fields(self):
        row = events.log("message", "bridge", "hello", board="my-app", task_id="t_1",
                         title="Timer", link="https://x/pull/1", spool=self.spool)
        lines = self.spool.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        got = json.loads(lines[0])
        for k in ("ts", "kind", "source", "text", "board", "task_id", "slug", "title", "link", "severity", "payload"):
            self.assertIn(k, got)
        self.assertEqual(got["kind"], "message"); self.assertEqual(got["severity"], "info")
        self.assertEqual(got, row)

    def test_never_raises_when_spool_dir_is_unwritable(self):
        bad = Path("/nonexistent-dir-for-test/events.jsonl")
        row = events.log("job", "run.sh", "x", payload={"job": "run.sh"}, spool=bad)  # must not raise
        self.assertEqual(row["kind"], "job")

    def test_payload_is_json_object(self):
        events.log("usage", "guard", "sample", payload={"session_pct": 42.0}, spool=self.spool)
        self.assertEqual(json.loads(self.spool.read_text())["payload"]["session_pct"], 42.0)

    def test_secrets_in_text_and_payload_are_scrubbed(self):
        token = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        openai_key = "sk-" + "a" * 24
        row = events.log("message", "x", f"token {token}",
                          payload={"body": openai_key}, spool=self.spool)
        raw = self.spool.read_text()
        self.assertNotIn(token, raw)
        self.assertNotIn(openai_key, raw)
        self.assertIn("<redacted:telegram-token>", raw)
        self.assertIn("<redacted:openai-key>", raw)
        self.assertEqual(row["text"], "token <redacted:telegram-token>")
        self.assertEqual(row["payload"]["body"], "<redacted:openai-key>")

    def test_secrets_in_nested_payload_are_scrubbed(self):
        openai_key = "sk-" + "b" * 24
        row = events.log("message", "x", "hello",
                          payload={"nested": {"a": [openai_key, "clean"]}}, spool=self.spool)
        raw = self.spool.read_text()
        self.assertNotIn(openai_key, raw)
        self.assertEqual(row["payload"]["nested"]["a"], ["<redacted:openai-key>", "clean"])

    def test_title_and_link_are_scrubbed(self):
        token = "123456789:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
        row = events.log("message", "x", "hi", title=f"leaked {token}",
                          link=f"https://x/{token}", spool=self.spool)
        self.assertNotIn(token, row["title"])
        self.assertNotIn(token, row["link"])
        self.assertIn("<redacted:telegram-token>", row["title"])
        self.assertIn("<redacted:telegram-token>", row["link"])


class Read(unittest.TestCase):
    def setUp(self):
        self.spool = Path(tempfile.mkdtemp()) / "events.jsonl"
        events.log("job", "run.sh", "old", payload={"job": "run.sh"}, spool=self.spool)
        # make the first row old
        rows = [json.loads(l) for l in self.spool.read_text().splitlines()]
        rows[0]["ts"] = time.time() - 3 * 86400
        self.spool.write_text(json.dumps(rows[0]) + "\n")
        events.log("job", "run.sh", "new", payload={"job": "run.sh"}, spool=self.spool)

    def test_read_all_and_since(self):
        self.assertEqual([r["text"] for r in events.read(self.spool)], ["old", "new"])
        self.assertEqual([r["text"] for r in events.read(self.spool, since_s=86400)], ["new"])

    def test_read_skips_corrupt_lines(self):
        with self.spool.open("a") as f:
            f.write("{not json\n")
        self.assertEqual(len(events.read(self.spool)), 2)


class NeedsMe(unittest.TestCase):
    def test_open_without_close_is_listed_closed_is_not(self):
        spool = Path(tempfile.mkdtemp()) / "e.jsonl"
        events.log("card", "bridge", "PR ready", task_id="t_a", title="A", payload={"event": "pr_ready", "state": "open"}, spool=spool)
        events.log("card", "bridge", "PR ready", task_id="t_b", title="B", payload={"event": "pr_ready", "state": "open"}, spool=spool)
        events.log("card", "bridge", "approved", task_id="t_b", payload={"event": "merged", "state": "closed"}, spool=spool)
        events.log("message", "bridge", "Spec ready", slug="s1", title="S", payload={"state": "open"}, spool=spool)
        open_ = events.needs_me(events.read(spool))
        self.assertEqual(sorted(r["title"] for r in open_), ["A", "S"])
        self.assertTrue(all("age_s" in r for r in open_))

    def test_reopened_after_close_is_listed(self):
        rows = [
            {"ts": 100, "kind": "card", "source": "bridge", "text": "opened", "board": None,
             "task_id": "X", "slug": None, "title": "first open", "link": None, "severity": "info",
             "payload": {"state": "open"}},
            {"ts": 200, "kind": "card", "source": "bridge", "text": "closed", "board": None,
             "task_id": "X", "slug": None, "title": "closed", "link": None, "severity": "info",
             "payload": {"state": "closed"}},
            {"ts": 300, "kind": "card", "source": "bridge", "text": "reopened", "board": None,
             "task_id": "X", "slug": None, "title": "reopened", "link": None, "severity": "info",
             "payload": {"state": "open"}},
        ]
        open_ = events.needs_me(rows)
        self.assertEqual(len(open_), 1)
        self.assertEqual(open_[0]["ts"], 300)
        self.assertEqual(open_[0]["title"], "reopened")


class Summary(unittest.TestCase):
    def test_counts_merged_and_failed(self):
        spool = Path(tempfile.mkdtemp()) / "e.jsonl"
        events.log("card", "bridge", "merged", title="Card one", payload={"event": "merged"}, spool=spool)
        events.log("job", "run.sh", "nightly exit 1", payload={"job": "nightly", "exit": 1}, spool=spool)
        events.log("job", "run.sh", "morning exit 0", payload={"job": "morning", "exit": 0}, spool=spool)
        s = events.summary(events.read(spool))
        self.assertEqual(s["counts"], {"card": 1, "job": 2})
        self.assertEqual(s["merged"], ["Card one"]); self.assertEqual(s["failed"], ["nightly"])


class Ship(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp()); self.spool = d / "e.jsonl"; self.off = d / "e.offset"
        events.log("job", "run.sh", "a", payload={"job": "run.sh"}, spool=self.spool); events.log("job", "run.sh", "b,with,commas", payload={"job": "run.sh"}, spool=self.spool)

    def test_ships_new_rows_and_advances_offset(self):
        sent = []
        n = events.ship(spool=self.spool, offset_path=self.off, copier=lambda csv: (sent.append(csv), True)[1])
        self.assertEqual(n, 2); self.assertEqual(len(sent), 1)
        self.assertIn('"b,with,commas"', sent[0])            # csv-quoted
        self.assertEqual(int(self.off.read_text()), self.spool.stat().st_size)
        self.assertEqual(events.ship(spool=self.spool, offset_path=self.off, copier=lambda csv: True), 0)

    def test_failed_copy_keeps_offset(self):
        n = events.ship(spool=self.spool, offset_path=self.off, copier=lambda csv: False)
        self.assertEqual(n, 0); self.assertFalse(self.off.exists())

    def test_ts_is_iso_and_payload_is_json(self):
        sent = []
        events.ship(spool=self.spool, offset_path=self.off, copier=lambda csv: (sent.append(csv), True)[1])
        first = sent[0].splitlines()[0]
        self.assertRegex(first, r",\d{4}-\d{2}-\d{2}T"); self.assertIn('""job""', first)

    def test_wrong_shaped_json_lines_are_skipped_not_raised(self):
        d = Path(tempfile.mkdtemp()); spool = d / "e.jsonl"; off = d / "e.offset"
        spool.write_text('"5"\n{}\n' + json.dumps({"ts": time.time(), "kind": "job", "source": "run.sh",
                                                     "text": "ok", "board": None, "task_id": None, "slug": None,
                                                     "title": None, "link": None, "severity": "info", "payload": {}}) + "\n")
        n = events.ship(spool=spool, offset_path=off, copier=lambda csv: True)
        self.assertEqual(n, 1)

    def test_control_characters_are_stripped_and_the_row_stays_one_row(self):
        d = Path(tempfile.mkdtemp()); spool = d / "e.jsonl"; off = d / "e.offset"
        events.log("job", "run.sh", "before\x00after\x07", title="ti\x1ftle",
                   link="http://x/\x0b", payload={"job": "run.sh"}, spool=spool)
        sent = []
        n = events.ship(spool=spool, offset_path=off,
                        copier=lambda csv: (sent.append(csv), True)[1])
        self.assertEqual(n, 1)
        self.assertEqual(len(sent[0].strip().splitlines()), 1)   # still exactly one CSV row
        for bad in ("\x00", "\x07", "\x1f", "\x0b"):
            self.assertNotIn(bad, sent[0])
        self.assertIn("beforeafter", sent[0]); self.assertIn("title", sent[0])

    def test_control_characters_already_in_the_spool_are_stripped_on_ship(self):
        d = Path(tempfile.mkdtemp()); spool = d / "e.jsonl"; off = d / "e.offset"
        spool.write_text(json.dumps({"ts": time.time(), "kind": "job", "source": "run.sh",
                                     "text": "nul\x00here", "board": None, "task_id": None,
                                     "slug": None, "title": None, "link": None,
                                     "severity": "info", "payload": {}}) + "\n")
        sent = []
        events.ship(spool=spool, offset_path=off,
                    copier=lambda csv: (sent.append(csv), True)[1])
        self.assertNotIn("\x00", sent[0]); self.assertIn("nulhere", sent[0])

    def test_unreadable_spool_returns_zero(self):
        d = Path(tempfile.mkdtemp())
        self.assertEqual(events.ship(spool=d, offset_path=d / "e.offset", copier=lambda csv: True), 0)


class CliLog(unittest.TestCase):
    def test_target_flag_lands_in_payload(self):
        spool = Path(tempfile.mkdtemp()) / "e.jsonl"
        old = events.SPOOL; events.SPOOL = spool
        try:
            events.main(["log", "message", "run.sh", "x", "--target", "telegram:1:2"])
            row = events.read(spool)[0]
            self.assertEqual(row["payload"]["target"], "telegram:1:2")
        finally:
            events.SPOOL = old


class NoSendGuard(unittest.TestCase):
    def test_psql_copy_is_inert_under_no_send(self):
        # AGENT_OPS_NO_SEND is set by every test module: the real copier must never call docker/psql.
        with unittest.mock.patch.object(events.subprocess, "run", side_effect=AssertionError("psql called")):
            self.assertTrue(events._psql_copy("a,b\n"))

    def test_test_process_without_the_marker_cannot_reach_live_postgres(self):
        # card rq_6dd2a60d: a unittest run missing AGENT_OPS_NO_SEND shipped board='test' rows
        # to the live ops-pg. Oracle 1: docker_env raises and _psql_copy reports it, no docker.
        # Oracle 2: ship() stays quiet-failing (returns 0, offset unmoved), so a tick cannot crash.
        spool = Path(tempfile.mkdtemp()) / "events.jsonl"
        events.log("job", "t", "x", payload={"job": "t"}, spool=spool)
        off = spool.with_suffix(".offset")
        gone = ("AGENT_OPS_NO_SEND", "AGENT_OPS_DOCKER_BIN")
        env = {k: v for k, v in os.environ.items() if k not in gone}
        boom = AssertionError("docker spawned")
        with unittest.mock.patch.dict(os.environ, env, clear=True), \
                unittest.mock.patch.object(events.subprocess, "run", side_effect=boom):
            with self.assertRaisesRegex(RuntimeError, "live ops-pg"):
                events.docker_env()
            err = io.StringIO()
            with unittest.mock.patch.object(sys, "stderr", err):
                self.assertFalse(events._psql_copy("a,b\n"))
            self.assertIn("live ops-pg", err.getvalue())
            self.assertEqual(events.ship(spool=spool, offset_path=off), 0)
        self.assertFalse(off.exists())

    def test_a_docker_override_still_reaches_the_fake(self):
        env = {k: v for k, v in os.environ.items() if k != "AGENT_OPS_NO_SEND"}
        stub = _stub_docker_dir()
        env["AGENT_OPS_DOCKER_BIN"] = stub
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            self.assertTrue(events.docker_env()["PATH"].startswith(stub + ":"))

    def test_a_docker_override_that_is_also_on_path_counts_as_live(self):
        # review F2b: any docker the process could reach (Homebrew, /usr/local) is the live CLI.
        real = Path(_stub_docker_dir())
        link = Path(tempfile.mkdtemp())
        (link / "docker").symlink_to(real / "docker")
        env = {k: v for k, v in os.environ.items() if k != "AGENT_OPS_NO_SEND"}
        env.update(AGENT_OPS_DOCKER_BIN=str(link), PATH=f"{real}:/usr/bin")
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "live ops-pg"):
                events.docker_env()

    def test_a_docker_override_dir_without_a_docker_does_not_bypass_the_guard(self):
        # /fake or an empty dir would fall through to the real CLI on PATH (review F2).
        for d in (tempfile.mkdtemp(), "/fake", events.DOCKER_BIN):
            env = {k: v for k, v in os.environ.items() if k != "AGENT_OPS_NO_SEND"}
            env["AGENT_OPS_DOCKER_BIN"] = d
            with unittest.mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(RuntimeError, "live ops-pg"):
                    events.docker_env()

if __name__ == "__main__":
    unittest.main()


class DefaultSpool(unittest.TestCase):
    """Belt and braces for the EVENTS_SPOOL-omission class: a test module that
    forgets the override must still never append to the live spool that
    events.ship copies into Postgres."""

    def _spool(self, env):
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            return events._default_spool()

    def test_no_send_without_override_is_not_the_repo_spool(self):
        live = Path(events.__file__).resolve().parent / "state" / "events.jsonl"
        p = self._spool({"AGENT_OPS_NO_SEND": "1"})
        self.assertNotEqual(p, live)
        self.assertFalse(str(p).startswith(str(live.parent)))

    def test_no_send_spool_env_gives_a_per_run_file(self):
        env = {"AGENT_OPS_NO_SEND": "1", "CAPSULE_NO_SEND_SPOOL": "/tmp/own-run.jsonl"}
        self.assertEqual(self._spool(env), Path("/tmp/own-run.jsonl"))
        env["EVENTS_SPOOL"] = "/tmp/x.jsonl"
        self.assertEqual(self._spool(env), Path("/tmp/x.jsonl"))

    def test_no_send_spool_env_is_ignored_without_no_send(self):
        """Production unchanged: a leaked CAPSULE_NO_SEND_SPOOL cannot redirect live events."""
        with unittest.mock.patch.object(Path, "home", return_value=Path("/h")):
            want = Path("/h/.local/state/capsule-corp/maint/events.jsonl")
            self.assertEqual(self._spool({"CAPSULE_NO_SEND_SPOOL": "/tmp/own-run.jsonl"}), want)

    def test_explicit_override_always_wins(self):
        self.assertEqual(self._spool({"AGENT_OPS_NO_SEND": "1", "EVENTS_SPOOL": "/tmp/x.jsonl"}),
                         Path("/tmp/x.jsonl"))

    def test_production_default_is_the_state_spool(self):
        with unittest.mock.patch.object(Path, "home", return_value=Path("/h")):
            want = Path("/h/.local/state/capsule-corp/maint/events.jsonl")
            self.assertEqual(self._spool({}), want)



class Envelope(unittest.TestCase):
    def setUp(self):
        self.spool = Path(tempfile.mkdtemp()) / "events.jsonl"

    def test_every_row_carries_v1_and_a_32_hex_event_id(self):
        row = events.log("health", "bridge", "sync tick", payload={"tick": True}, spool=self.spool)
        self.assertEqual(row["v"], 1)
        self.assertRegex(row["event_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(list(row)[:2], ["v", "event_id"])   # front of the row, for a reader
        self.assertEqual(json.loads(self.spool.read_text())["event_id"], row["event_id"])

    def test_event_ids_are_unique_across_calls(self):
        ids = {events.log("audit", "s", str(i), spool=self.spool)["event_id"] for i in range(50)}
        self.assertEqual(len(ids), 50)

    def test_the_eleven_legacy_keys_are_untouched(self):
        row = events.log("message", "bridge", "hi", board="b", task_id="t", slug="s",
                         title="T", link="L", severity="warn", payload={"x": 1}, spool=self.spool)
        for k in ("ts", "kind", "source", "board", "task_id", "slug", "title",
                  "text", "link", "severity", "payload"):
            self.assertIn(k, row)

    def test_a_caller_may_not_supply_its_own_event_id(self):
        with self.assertRaises(TypeError):
            events.log("audit", "s", "x", event_id="deadbeef", spool=self.spool)


class Validation(unittest.TestCase):
    def setUp(self):
        self.spool = Path(tempfile.mkdtemp()) / "events.jsonl"

    def test_the_kinds_table_covers_every_kind_in_use(self):
        self.assertEqual(set(events.KINDS), {"card", "job", "health", "audit", "message",
                                             "usage", "vault", "ai_usage",
                                             "backup_done", "backup_partial",
                                             "ledger_mismatch", "infra", "release", "dep_cache",
                                             "tick_health", "capsule_unblocked", "host_gap",
                                         "tick_stale_restart"})
        self.assertEqual(events.KINDS["card"], ("event",))
        self.assertEqual(events.KINDS["ledger_mismatch"], ("class", "board"))
        self.assertEqual(events.KINDS["job"], ("job",))
        self.assertEqual(events.KINDS["health"], ("tick",))
        self.assertEqual(events.KINDS["infra"], ("component",))
        self.assertEqual(events.KINDS["tick_health"], ("pass", "pid"))

    def test_strict_is_on_under_no_send_and_under_the_explicit_flag(self):
        with unittest.mock.patch.dict(os.environ, {"AGENT_OPS_NO_SEND": "1"}, clear=True):
            self.assertTrue(events._strict())
        with unittest.mock.patch.dict(os.environ, {"AGENT_OPS_EVENTS_STRICT": "1"}, clear=True):
            self.assertTrue(events._strict())
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(events._strict())

    def test_unknown_kind_raises_under_strict(self):
        with self.assertRaises(events.EventInvalid):
            events.log("wat", "s", "x", spool=self.spool)   # the suite runs with AGENT_OPS_NO_SEND

    def test_missing_required_payload_key_raises_under_strict(self):
        with self.assertRaises(events.EventInvalid):
            events.log("card", "s", "x", payload={"state": "open"}, spool=self.spool)
        with self.assertRaises(events.EventInvalid):
            events.log("card", "s", "x", payload={"event": ""}, spool=self.spool)   # empty is missing

    def test_dep_cache_row_without_event_or_repo_raises_under_strict(self):
        with self.assertRaises(events.EventInvalid):
            events.log("dep_cache", "s", "x", payload={"repo": "my-app"}, spool=self.spool)
        with self.assertRaises(events.EventInvalid):
            events.log("dep_cache", "s", "x", payload={"event": "hit"}, spool=self.spool)

    def test_dep_cache_miss_row_is_valid_and_can_carry_warn_severity(self):
        row = events.log("dep_cache", "bridge", "pip:httpx==0.27.2 not allowlisted",
                         payload={"event": "miss", "repo": "my-app"}, severity="warn",
                         spool=self.spool)
        self.assertEqual(row["kind"], "dep_cache")
        self.assertEqual(row["severity"], "warn")
        self.assertEqual(row["payload"]["event"], "miss")

    def test_without_strict_it_warns_on_stderr_and_still_writes_the_row(self):
        err = io.StringIO()
        with unittest.mock.patch.dict(os.environ, {}, clear=True), \
             unittest.mock.patch("sys.stderr", err):
            row = events.log("card", "s", "x", payload={"state": "open"}, spool=self.spool)
        self.assertIn("events: invalid", err.getvalue())
        self.assertEqual(len(self.spool.read_text().splitlines()), 1)
        self.assertEqual(row["payload"], {"state": "open"})

    def test_infra_kind_requires_component(self):
        with self.assertRaises(events.EventInvalid):
            events.log("infra", "heal", "docker ok", payload={"ok": True}, spool=self.spool)
        events.log("infra", "heal", "docker ok",
                   payload={"component": "docker", "ok": True, "fails": 0, "first_fail": None},
                   spool=self.spool)

    def test_log_still_never_raises_on_an_unwritable_spool(self):
        row = events.log("job", "run.sh", "x", payload={"job": "x"},
                         spool=Path("/nonexistent-dir-for-test/events.jsonl"))
        self.assertEqual(row["kind"], "job")


class ReleaseKind(unittest.TestCase):
    def setUp(self):
        self.spool = Path(tempfile.mkdtemp()) / "events.jsonl"

    def test_release_requires_repo_and_version(self):
        """Row 121 reserved the kind with no payload keys; row 138 (C10) ships
        the emitter and requires two. A release row without them is invalid."""
        self.assertEqual(events.KINDS["release"], ("repo", "version"))
        with self.assertRaises(events.EventInvalid):
            events.log("release", "release_gate", "x", payload={"repo": "my-app"})
        with self.assertRaises(events.EventInvalid):
            events.log("release", "release_gate", "x", payload={"version": "0.1.0"})
        row = events.log("release", "release_gate", "released my-app v0.1.0",
                         payload={"repo": "my-app", "version": "0.1.0",
                                  "tag": "v0.1.0", "sha": "0" * 40}, spool=self.spool)
        self.assertEqual(row["payload"]["tag"], "v0.1.0")

    def test_release_has_exactly_one_emitter(self):
        """C1's rule kept, one file wider: nothing but the release gate may
        invent a release row. Distinct from payload.event == "released", which
        is the card event a (since removed) bridge step used to emit."""
        allowed = {"release_gate.py"}
        py_needle = "events.log(" + '"release"'
        sh_needles = ("log release", 'log "release"', "ev_log release")
        hits = []
        for p in sorted((ROOT / "maintenance").rglob("*.py")) + sorted((ROOT / "maintenance").rglob("*.sh")):
            if p.name in allowed or "/tests/" in str(p):
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            if py_needle in text or any(n in text for n in sh_needles):
                hits.append(str(p.relative_to(ROOT)))
        self.assertEqual(hits, [], f"unexpected release emitter in {hits}")
        rg = (ROOT / "maintenance" / "release_gate.py").read_text(encoding="utf-8")
        self.assertIn(py_needle, rg)


class ShipEnvelope(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp()); self.spool = d / "e.jsonl"; self.off = d / "e.offset"

    def test_v_and_event_id_are_the_first_two_csv_columns(self):
        row = events.log("job", "run.sh", "a", payload={"job": "x"}, spool=self.spool)
        sent = []
        events.ship(spool=self.spool, offset_path=self.off,
                    copier=lambda csv: (sent.append(csv), True)[1])
        self.assertTrue(sent[0].startswith(f"1,{row['event_id']},"))
        self.assertTrue(events.COPY_COLS.startswith("v,event_id,ts,"))

    def test_a_legacy_row_without_the_envelope_ships_as_v1_and_an_empty_event_id(self):
        self.spool.write_text(json.dumps({"ts": time.time(), "kind": "job", "source": "run.sh",
                                          "text": "old", "board": None, "task_id": None,
                                          "slug": None, "title": None, "link": None,
                                          "severity": "info", "payload": {}}) + "\n")
        sent = []
        n = events.ship(spool=self.spool, offset_path=self.off,
                        copier=lambda csv: (sent.append(csv), True)[1])
        self.assertEqual(n, 1)
        self.assertTrue(sent[0].startswith("1,,"))   # FORCE_NULL turns the empty field into NULL

    def test_the_csv_ship_hands_its_copier_has_no_carriage_returns(self):
        # C1: csv.writer's default dialect terminates rows with CRLF. The old
        # code passed the CSV as psql's stdin DATA stream for `-c "COPY ...
        # FROM STDIN"`, which the server accepted. `_stage_script` embeds the
        # same bytes as COPY data inside a script read by `psql -f -`, and
        # psql's script-mode copy handler rejects a CRLF data line outright:
        # `ERROR: unquoted newline found in data`. Every batch failed, ship()
        # returned 0, the offset never advanced and Postgres saw no events.
        for i in range(3):
            events.log("job", "run.sh", f"row {i}", payload={"job": "x"}, spool=self.spool)
        sent = []
        n = events.ship(spool=self.spool, offset_path=self.off,
                        copier=lambda csv_text: (sent.append(csv_text), True)[1])
        self.assertEqual(n, 3)
        self.assertNotIn("\r", sent[0])
        self.assertEqual(len(sent[0].splitlines()), 3)

    def test_a_row_with_a_comma_a_quote_and_a_newline_is_csv_quoted_not_hand_rolled(self):
        # I4: the copier must receive csv-module output, so the separator,
        # the doubled quote and the embedded newline are all quoted properly -
        # an embedded newline inside a quoted field is legal COPY csv data and
        # must NOT be normalised away with the row terminator.
        nasty = 'a, b "c" d\ne'
        events.log("job", "run.sh", nasty, title=nasty, payload={"job": "x"}, spool=self.spool)
        sent = []
        events.ship(spool=self.spool, offset_path=self.off,
                    copier=lambda csv_text: (sent.append(csv_text), True)[1])
        self.assertNotIn("\r", sent[0])
        row = next(iter(csv.reader(io.StringIO(sent[0]))))
        # `_clean` collapses whitespace in title/text, so compare the quoting
        # machinery on a payload field, which is shipped as raw JSON.
        self.assertIn('"', sent[0])
        self.assertEqual(row[3], "job")
        self.assertEqual(json.loads(row[12])["job"], "x")
        self.assertEqual(len(row), 13)

    def test_a_payload_holding_a_quote_and_a_newline_round_trips_through_csv(self):
        events.log("job", "run.sh", "t", payload={"job": "x", "note": 'he said "hi",\nthen left'},
                   spool=self.spool)
        sent = []
        events.ship(spool=self.spool, offset_path=self.off,
                    copier=lambda csv_text: (sent.append(csv_text), True)[1])
        self.assertNotIn("\r", sent[0])
        rows = list(csv.reader(io.StringIO(sent[0])))
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0][12])["note"], 'he said "hi",\nthen left')

    def test_the_psql_script_never_carries_a_crlf_data_line(self):
        # Belt-and-braces: even if a copier is handed CRLF text from
        # somewhere, the script psql reads must not contain it.
        script = events._stage_script("1,abc,2026-01-01T00:00:00+00:00,job,s,,,,,,info,{}\r\n")
        self.assertNotIn("\r", script)

    def test_the_psql_script_stages_then_inserts_on_conflict_do_nothing(self):
        seen = {}

        class R:
            returncode = 0
            stderr = ""

        def fake_run(argv, **kw):
            seen["argv"] = argv; seen["input"] = kw.get("input")
            return R()

        stub_env = {"AGENT_OPS_DOCKER_BIN": _stub_docker_dir()}
        with unittest.mock.patch.dict(os.environ, stub_env, clear=True), \
             unittest.mock.patch("subprocess.run", fake_run):
            self.assertTrue(events._psql_copy("1,abc,2026-01-01T00:00:00+00:00,job,s,,,,,,info,{}\n"))
        script = seen["input"]
        self.assertIn("CREATE TEMP TABLE events_stage (LIKE events INCLUDING DEFAULTS)", script)
        self.assertIn(f"COPY events_stage({events.COPY_COLS}) FROM STDIN "
                      "WITH (FORMAT csv, FORCE_NULL (event_id))", script)
        self.assertIn("\n\\.\n", script)
        self.assertIn("ON CONFLICT (event_id) WHERE event_id IS NOT NULL DO NOTHING", script)
        self.assertIn("-v", seen["argv"]); self.assertIn("ON_ERROR_STOP=1", seen["argv"])

    def test_a_reshipped_row_is_inserted_once(self):
        """The offset stays the primary defence; this is the backstop for a COPY
        that lands while the offset write fails."""
        table = set()

        def copier(csv_text):
            for line in csv_text.splitlines():
                eid = line.split(",")[1]
                if eid and eid in table:
                    continue           # ON CONFLICT ... DO NOTHING
                table.add(eid)
            return True

        events.log("job", "run.sh", "a", payload={"job": "x"}, spool=self.spool)
        events.ship(spool=self.spool, offset_path=self.off, copier=copier)
        self.off.unlink()              # the offset write failed
        events.ship(spool=self.spool, offset_path=self.off, copier=copier)
        self.assertEqual(len(table), 1)
