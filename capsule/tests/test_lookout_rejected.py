"""t_69633914: the Lookout `spool.rejected` sidecar is capped and rotated (mirrors maintain_spool),
a non-zero quarantined count raises a notice-level Grafana alert through the existing `job` event
-> ops-pg -> rules.yaml path, and the r2/r3 review follow-ups (content dedupe, prices error quoted).
Oracle 1: the shipper through the fake docker (`ShipCase`). Oracle 2: the rule's own rawSql is read
back out of rules.yaml and cross-checked against the event the shipper really emitted; the same
rule's intent examples (alerts.jsonl) are evaluated on real Postgres by tests/test_migration_pg.sh."""
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

from capsule import lookout, lookout_ship, paths
from capsule.tests.public_tree import private_only
from capsule.tests.test_lookout_ship import INTENT, ShipCase, ev

RULES = paths.REPO / "observability" / "grafana" / "provisioning" / "alerting" / "rules.yaml"
RUNBOOK = paths.REPO / "docs" / "RUNBOOK.md"
JOB = "lookout-quarantine"


def rule_block(uid):
    hits = [b for b in re.split(r"(?m)^(?=      - uid: )", RULES.read_text()) if b.startswith(f"      - uid: {uid}\n")]
    return hits[0] if hits else ""


class Rejected(ShipCase):
    def ship_err(self, **kw):
        err, old = io.StringIO(), sys.stderr
        sys.stderr = err
        try:
            rc = lookout_ship.ship(self.home, **kw)
        finally:
            sys.stderr = old
        return rc, err.getvalue()

    def rej(self, name="spool.rejected"):
        p = lookout.lookout_dir(self.home) / name
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def test_intent_rejected_sidecar_is_capped_and_rotated(self):
        """Oracle 1: 8 bad rows overflow a 3000-byte cap once: the older entries rotate to
        spool.rejected.1, none is lost, the live file stays under the cap. Oracle 2: 30 bad rows
        in one ship keep both generations under the cap (bounded, not growing with the input)."""
        old = lookout.REJECTED_MAX_BYTES
        lookout.REJECTED_MAX_BYTES = 3000
        self.addCleanup(setattr, lookout, "REJECTED_MAX_BYTES", old)
        self.write([ev(1), ev(2), ev(3)] + [ev(10 + i) for i in range(8)])
        os.environ["FAKE_PSQL_REJECT_IF_CONTAINS"] = "|".join(f"e{10 + i}" for i in range(8))
        self.assertEqual(lookout_ship.ship(self.home), 0)
        live, rotated = self.rej(), self.rej("spool.rejected.1")
        self.assertTrue(rotated)
        self.assertEqual(len(live) + len(rotated), 8)
        self.assertLessEqual(lookout.rejected_path(self.home).stat().st_size, 3000)
        self.write([ev(4), ev(5)] + [ev(100 + i) for i in range(30)])
        os.environ["FAKE_PSQL_REJECT_IF_CONTAINS"] = "|".join(f"e{100 + i}" for i in range(30))
        self.assertEqual(lookout_ship.ship(self.home), 0)
        for name in ("spool.rejected", "spool.rejected.1"):
            self.assertLessEqual((lookout.lookout_dir(self.home) / name).stat().st_size, 3000, name)

    def test_intent_rejected_dedupes_on_row_content_not_the_span(self):
        """r2 Minor 6: maintain_spool compaction rewrites the spool, so a surviving bad row gets a
        new byte span. Oracle 1: re-shipping it from a different offset adds no second entry.
        Oracle 2: a different bad row still lands (the dedupe is not 'ignore everything')."""
        self.write([ev(1), ev(2), ev(3)])
        os.environ["FAKE_PSQL_REJECT_IF_CONTAINS"] = "e2|e9"
        self.assertEqual(lookout_ship.ship(self.home), 0)
        sp = lookout.spool_path(self.home)
        sp.write_text("")
        lookout.offset_path(self.home).write_text("0")           # what maintain_spool leaves behind
        self.write([ev(4), ev(5), ev(2), ev(6), ev(9)])
        self.assertEqual(lookout_ship.ship(self.home), 0)
        lines = [r["line"] for r in self.rej()]
        self.assertEqual(sum('"e2"' in l for l in lines), 1)
        self.assertEqual(sum('"e9"' in l for l in lines), 1)

    def test_intent_prices_and_bad_row_quotes_the_prices_error(self):
        """r3 Minor 2: in the prices AND bad-row case the 'prices failed' line must quote the
        prices error (first attempt), not the row error from the prices-free retry."""
        self.write([ev(1), ev(2), ev(3)])
        os.environ["FAKE_PSQL_REJECT_IF_CONTAINS"] = "insert into prices|e2"
        rc, err = self.ship_err(full=True)
        self.assertEqual(rc, 0)
        line = next(l for l in err.splitlines() if "prices failed" in l)
        self.assertIn("(insert into prices)", line)
        self.assertNotIn("(e2)", line)

    def test_intent_quarantine_raises_one_lookout_alert_event(self):
        """Oracle 1: a quarantine writes one `job` event (exit 0, so `job-failed` stays quiet)
        carrying the count. Oracle 2: the event's job name is the one the rule's rawSql reads."""
        spool = self.home / "events.jsonl"
        self.write([ev(1), ev(2), ev(3)])
        os.environ["FAKE_PSQL_REJECT_IF_CONTAINS"] = "e2"
        self.assertEqual(lookout_ship.ship(self.home, events_spool=spool), 0)
        rows = [json.loads(l) for l in spool.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["kind"], rows[0]["severity"]), ("job", "warn"))
        self.assertEqual((rows[0]["payload"]["job"], rows[0]["payload"]["exit"], rows[0]["payload"]["quarantined"]),
                         (JOB, 0, 1))
        self.assertIn(f"payload->>'job'='{JOB}'", rule_block("lookout-quarantined"))

    def test_intent_clean_ship_raises_no_alert_event(self):
        """The alert is only for a non-zero quarantine: a clean ship writes no event."""
        spool = self.home / "events.jsonl"
        self.write([ev(1), ev(2)])
        self.assertEqual(lookout_ship.ship(self.home, events_spool=spool), 0)
        self.assertFalse(spool.exists() and spool.read_text().strip())

    @private_only
    def test_intent_quarantined_rule_is_a_notice_with_a_runbook_section(self):
        """Oracle 1: the rule exists as a `notice` on ops-pg that sums the event's count over 24 h.
        Oracle 2: its runbook anchor is a real RUNBOOK heading, and alerts.jsonl pins a fires and a
        quiet case with a current SHA256SUMS line."""
        b = rule_block("lookout-quarantined")
        self.assertIn("severity: notice", b)
        self.assertIn("datasourceUid: ops-pg", b)
        self.assertIn("interval '24 hours'", b)
        anchor = re.search(r"runbook: docs/RUNBOOK.md#(\S+)", b).group(1)
        slugs = {re.sub(r"[^\w-]", "", h.strip().lower().replace(" ", "-"))
                 for h in re.findall(r"(?m)^#{2,4} (.+)$", RUNBOOK.read_text())}
        self.assertIn(anchor, slugs)
        text = (INTENT / "alerts.jsonl").read_text()
        cases = [json.loads(l) for l in text.splitlines() if json.loads(l)["rule"] == "lookout-quarantined"]
        self.assertEqual({c["expect"] for c in cases}, {"fires", "quiet"})
        sums = dict(reversed(l.split(maxsplit=1)) for l in (INTENT / "SHA256SUMS").read_text().splitlines())
        self.assertEqual(sums["alerts.jsonl"], hashlib.sha256(text.encode()).hexdigest())

    def test_status_survives_the_sidecar_vanishing_between_exists_and_read(self):
        """Review Minor 1: rotation can remove the file between status()'s exists and read; an
        unreadable sidecar (a directory here: exists() is true, read raises OSError) counts 0."""
        lookout.rejected_path(self.home).mkdir()
        self.assertEqual(lookout_ship.status(self.home)["rejected"], 0)

    def test_file_matches_tests(self):
        text = (INTENT / "rejected.jsonl").read_text()
        sums = dict(reversed(l.split(maxsplit=1)) for l in (INTENT / "SHA256SUMS").read_text().splitlines())
        self.assertEqual(sums["rejected.jsonl"], hashlib.sha256(text.encode()).hexdigest())
        ids = {json.loads(l)["id"].replace("-", "_") for l in text.splitlines()}
        self.assertEqual(ids, {n[len("test_intent_"):] for n in dir(Rejected) if n.startswith("test_intent_")})


if __name__ == "__main__":
    unittest.main()
