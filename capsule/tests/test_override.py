"""The `override` verb (row 454 item 4): grant, review-stage skip, expiry, irreversible
refusal, repeat refusal, unknown-check refusal, TTL cap, fix-card validation, report totals.
Tempfile board, fake clock (`now=` throughout, never `time.time()`)."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from capsule import db, identity, override

CFG = {"operator": "owner", "irreversible_checks": ["merge", "deploy", "give-back"],
       "contract_commands": ["lint", "unit-tests", "merge", "deploy", "give-back"]}


class OverrideCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.board_path = self.home / "board.db"
        db.create_board(self.board_path)
        self.conn = db.connect(self.board_path)
        self.tid = db.new_task(self.conn, title="fix wobble", body="", branch="epic/x")
        self.fix = db.new_task(self.conn, title="fix card", body="", branch="epic/x")
        # C2: `grant` re-verifies `issuer` against a real identity file every time.
        self.identity_dir = self.home / "identities"
        self.identity_dir.mkdir()
        for name in ("fable", "owner"):
            f = self.identity_dir / f"{name}.env"
            f.write_text("x\n")
            os.chmod(f, 0o600)
        self._old_identity_dir = os.environ.get("CAPSULE_IDENTITY_DIR")
        os.environ["CAPSULE_IDENTITY_DIR"] = str(self.identity_dir)

    def tearDown(self):
        self.conn.close()
        if self._old_identity_dir is None:
            os.environ.pop("CAPSULE_IDENTITY_DIR", None)
        else:
            os.environ["CAPSULE_IDENTITY_DIR"] = self._old_identity_dir

    def events(self, kind):
        return self.conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
                                 (self.tid, kind)).fetchall()

    def outbox_lines(self):
        return [r["body"] for r in self.conn.execute("SELECT body FROM outbox ORDER BY id")]


class ParseTtl(unittest.TestCase):
    def test_hours_days_minutes_seconds(self):
        self.assertEqual(override.parse_ttl("72h"), 72 * 3600)
        self.assertEqual(override.parse_ttl("2d"), 2 * 86400)
        self.assertEqual(override.parse_ttl("30m"), 30 * 60)
        self.assertEqual(override.parse_ttl("45s"), 45)
        self.assertEqual(override.parse_ttl("45"), 45)   # bare number: seconds

    def test_rejects_bad_ttl(self):
        for bad in ("", "0h", "-1h", "abc", "12x"):
            with self.assertRaises(ValueError):
                override.parse_ttl(bad)


class Grant(OverrideCase):
    def test_grant_records_event_and_outbox(self):
        ok, why = override.grant(self.conn, CFG, "test", self.tid, check="lint", reason="flaky rule",
                                  ttl="72h", fix_card=self.fix, issuer="fable", now=1000)
        self.assertTrue(ok)
        self.assertIsNone(why)
        rows = self.events("override")
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0]["payload"])
        self.assertEqual(payload["check"], "lint")
        self.assertEqual(payload["reason"], "flaky rule")
        self.assertEqual(payload["expires_at"], 1000 + 72 * 3600)
        self.assertEqual(payload["fix_card"], self.fix)
        self.assertEqual(payload["issuer"], "fable")
        self.assertEqual(payload["board"], "test")
        self.assertTrue(any("override granted" in b and "lint" in b for b in self.outbox_lines()))

    def test_missing_fields_refused(self):
        ok, why = override.grant(self.conn, CFG, "test", self.tid, check="", reason="x",
                                  ttl="72h", fix_card=self.fix, issuer="fable", now=1)
        self.assertFalse(ok)
        self.assertIn("needs", why)
        self.assertEqual(len(self.events("override_refused")), 1)

    def test_blank_reason_refused(self):
        ok, why = override.grant(self.conn, CFG, "test", self.tid, check="lint", reason="   ",
                                  ttl="72h", fix_card=self.fix, issuer="fable", now=1)
        self.assertFalse(ok)


class UnknownCheck(OverrideCase):
    """I3: the check id must name a real contract command (by text or index); anything else is
    refused at grant time, never discovered later as a bypass that quietly never applied."""

    def test_unknown_check_id_refused(self):
        ok, why = override.grant(self.conn, CFG, "test", self.tid, check="typo-check", reason="x",
                                  ttl="72h", fix_card=self.fix, issuer="fable", now=1)
        self.assertFalse(ok)
        self.assertIn("matches no contract command", why)

    def test_index_form_accepted(self):
        ok, _ = override.grant(self.conn, CFG, "test", self.tid, check="0", reason="x",
                               ttl="72h", fix_card=self.fix, issuer="fable", now=1)
        self.assertTrue(ok)


class IndexNormalization(OverrideCase):
    """Fix round 2, I-A: an index-form check id is normalised to its command's text before it is
    stored, matched, or used as the repeat key - never a bypass that silently never applies."""

    def test_granted_by_index_is_stored_as_text(self):
        override.grant(self.conn, CFG, "test", self.tid, check="1", reason="x",   # index 1 = 'unit-tests'
                       ttl="72h", fix_card=self.fix, issuer="fable", now=1000)
        payload = json.loads(self.events("override")[0]["payload"])
        self.assertEqual(payload["check"], "unit-tests")

    def test_granted_by_index_then_applies_against_the_text_form(self):
        override.grant(self.conn, CFG, "test", self.tid, check="1", reason="x",
                       ttl="72h", fix_card=self.fix, issuer="fable", now=1000)
        ov = override.active(self.conn, self.tid, "unit-tests", now=1001)
        self.assertIsNotNone(ov)

    def test_index_then_text_form_share_the_same_14_day_repeat_key(self):
        override.grant(self.conn, CFG, "test", self.tid, check="1", reason="first",
                       ttl="72h", fix_card=self.fix, issuer="fable", now=1000)
        other = db.new_task(self.conn, title="other", body="", branch="epic/x")
        ok, why = override.grant(self.conn, CFG, "test", other, check="unit-tests", reason="second",
                                 ttl="72h", fix_card=self.fix, issuer="fable", now=1001)
        self.assertFalse(ok)
        self.assertIn("contract must change", why)


class IssuerVerification(OverrideCase):
    """Fix round 2, C2: the tick re-verifies `issuer` against its identity file every grant."""

    def test_unresolvable_issuer_is_refused(self):
        ok, why = override.grant(self.conn, CFG, "test", self.tid, check="lint", reason="x",
                                  ttl="72h", fix_card=self.fix, issuer="nobody-registered", now=1)
        self.assertFalse(ok)
        self.assertIn("no resolvable identity file", why)
        self.assertIsNone(identity.verify("nobody-registered", identity_dir=self.identity_dir))

    def test_the_stored_issuer_is_the_verified_name(self):
        ok, _ = override.grant(self.conn, CFG, "test", self.tid, check="lint", reason="x",
                               ttl="72h", fix_card=self.fix, issuer="fable", now=1)
        self.assertTrue(ok)
        payload = json.loads(self.events("override")[0]["payload"])
        self.assertEqual(payload["issuer"], "fable")


class TtlCap(OverrideCase):
    def test_over_the_cap_refused(self):
        ok, why = override.grant(self.conn, CFG, "test", self.tid, check="lint", reason="x",
                                  ttl="8d", fix_card=self.fix, issuer="fable", now=1)
        self.assertFalse(ok)
        self.assertIn("cap", why)

    def test_at_the_default_cap_accepted(self):
        ok, _ = override.grant(self.conn, CFG, "test", self.tid, check="lint", reason="x",
                               ttl="7d", fix_card=self.fix, issuer="fable", now=1)
        self.assertTrue(ok)

    def test_configured_cap_enforced(self):
        cfg = dict(CFG, override_max_ttl=3600)
        ok, why = override.grant(self.conn, cfg, "test", self.tid, check="lint", reason="x",
                                 ttl="2h", fix_card=self.fix, issuer="fable", now=1)
        self.assertFalse(ok)
        ok, _ = override.grant(self.conn, cfg, "test", self.tid, check="lint", reason="x",
                               ttl="1h", fix_card=self.fix, issuer="fable", now=1)
        self.assertTrue(ok)


class FixCardValidation(OverrideCase):
    def test_missing_fix_card_refused(self):
        ok, why = override.grant(self.conn, CFG, "test", self.tid, check="lint", reason="x",
                                  ttl="72h", fix_card="t_nope", issuer="fable", now=1)
        self.assertFalse(ok)
        self.assertIn("fix card", why)

    def test_fix_card_cannot_be_the_overridden_card_itself(self):
        ok, why = override.grant(self.conn, CFG, "test", self.tid, check="lint", reason="x",
                                  ttl="72h", fix_card=self.tid, issuer="fable", now=1)
        self.assertFalse(ok)

    def test_done_fix_card_refused(self):
        self.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (self.fix,))
        ok, why = override.grant(self.conn, CFG, "test", self.tid, check="lint", reason="x",
                                  ttl="72h", fix_card=self.fix, issuer="fable", now=1)
        self.assertFalse(ok)


class ReviewSkip(OverrideCase):
    def test_active_returns_payload_before_expiry(self):
        override.grant(self.conn, CFG, "test", self.tid, check="lint", reason="flaky",
                       ttl="72h", fix_card=self.fix, issuer="fable", now=1000)
        ov = override.active(self.conn, self.tid, "lint", now=1000 + 3600)
        self.assertIsNotNone(ov)
        self.assertEqual(ov["reason"], "flaky")

    def test_inactive_for_a_different_check(self):
        override.grant(self.conn, CFG, "test", self.tid, check="lint", reason="flaky",
                       ttl="72h", fix_card=self.fix, issuer="fable", now=1000)
        self.assertIsNone(override.active(self.conn, self.tid, "unit-tests", now=1001))

    def test_inactive_for_a_different_task(self):
        override.grant(self.conn, CFG, "test", self.tid, check="lint", reason="flaky",
                       ttl="72h", fix_card=self.fix, issuer="fable", now=1000)
        other = db.new_task(self.conn, title="other", body="", branch="epic/x")
        self.assertIsNone(override.active(self.conn, other, "lint", now=1001))


class Expiry(OverrideCase):
    def test_expired_override_is_ignored(self):
        override.grant(self.conn, CFG, "test", self.tid, check="lint", reason="flaky",
                       ttl="1h", fix_card=self.fix, issuer="fable", now=1000)
        expires_at = 1000 + 3600
        self.assertIsNotNone(override.active(self.conn, self.tid, "lint", now=expires_at - 1))
        self.assertIsNone(override.active(self.conn, self.tid, "lint", now=expires_at))
        self.assertIsNone(override.active(self.conn, self.tid, "lint", now=expires_at + 100))

    def test_note_expiry_writes_one_event_and_outbox_line_then_is_idempotent(self):
        override.grant(self.conn, CFG, "test", self.tid, check="lint", reason="flaky",
                       ttl="1h", fix_card=self.fix, issuer="fable", now=1000)
        payload = override.last(self.conn, self.tid, "lint")
        expires_at = 1000 + 3600
        announced = override.note_expiry(self.conn, self.tid, "lint", payload, now=expires_at + 1)
        self.assertTrue(announced)
        self.assertEqual(len(self.events("override_expired")), 1)
        self.assertTrue(any("expired" in b for b in self.outbox_lines()))
        announced_again = override.note_expiry(self.conn, self.tid, "lint", payload, now=expires_at + 500)
        self.assertFalse(announced_again)
        self.assertEqual(len(self.events("override_expired")), 1)


class IrreversibleRefusal(OverrideCase):
    def test_non_operator_refused_on_irreversible_check(self):
        ok, why = override.grant(self.conn, CFG, "test", self.tid, check="merge", reason="ship it",
                                  ttl="72h", fix_card=self.fix, issuer="fable", now=1)
        self.assertFalse(ok)
        self.assertIn("irreversible", why)
        self.assertIsNone(override.active(self.conn, self.tid, "merge", now=2))
        self.assertEqual(len(self.events("override_refused")), 1)
        self.assertEqual(len(self.events("override")), 0)

    def test_operator_allowed_on_irreversible_check(self):
        ok, why = override.grant(self.conn, CFG, "test", self.tid, check="merge", reason="ship it",
                                  ttl="72h", fix_card=self.fix, issuer="owner", now=1)
        self.assertTrue(ok)
        self.assertIsNotNone(override.active(self.conn, self.tid, "merge", now=2))

    def test_no_operator_configured_refuses_everyone(self):
        cfg = dict(CFG, operator=None)
        ok, why = override.grant(self.conn, cfg, "test", self.tid, check="deploy", reason="ship it",
                                  ttl="72h", fix_card=self.fix, issuer="owner", now=1)
        self.assertFalse(ok)

    def test_default_irreversible_list_without_config_override(self):
        cfg = {"operator": "owner", "contract_commands": CFG["contract_commands"]}
        ok, _ = override.grant(self.conn, cfg, "test", self.tid, check="give-back", reason="x",
                               ttl="72h", fix_card=self.fix, issuer="fable", now=1)
        self.assertFalse(ok)
        ok, _ = override.grant(self.conn, cfg, "test", self.tid, check="lint", reason="x",
                               ttl="72h", fix_card=self.fix, issuer="fable", now=1)
        self.assertTrue(ok)

    def test_irreversible_checks_must_be_a_list_not_a_string(self):
        """M3: a misconfigured string (e.g. 'merge') must never silently become the character set
        {'m','e','r','g'} - it falls back to the default list, so 'merge' stays irreversible."""
        cfg = dict(CFG, irreversible_checks="merge")
        ok, why = override.grant(self.conn, cfg, "test", self.tid, check="merge", reason="x",
                                 ttl="72h", fix_card=self.fix, issuer="fable", now=1)
        self.assertFalse(ok)
        self.assertIn("irreversible", why)


class RepeatRefusal(OverrideCase):
    def test_second_override_same_check_within_14_days_refused(self):
        ok1, _ = override.grant(self.conn, CFG, "test", self.tid, check="lint", reason="first",
                                ttl="72h", fix_card=self.fix, issuer="fable", now=1000)
        self.assertTrue(ok1)
        other = db.new_task(self.conn, title="other card", body="", branch="epic/x")
        ok2, why2 = override.grant(self.conn, CFG, "test", other, check="lint", reason="second",
                                   ttl="72h", fix_card=self.fix, issuer="fable", now=1000 + 3600)
        self.assertFalse(ok2)
        self.assertIn("contract must change", why2)

    def test_repeat_refused_even_after_first_expired(self):
        override.grant(self.conn, CFG, "test", self.tid, check="lint", reason="first",
                       ttl="1h", fix_card=self.fix, issuer="fable", now=1000)
        just_under_14d = 1000 + override.REPEAT_WINDOW_S - 1
        ok, why = override.grant(self.conn, CFG, "test", self.tid, check="lint", reason="second",
                                 ttl="1h", fix_card=self.fix, issuer="fable", now=just_under_14d)
        self.assertFalse(ok)
        self.assertIn("contract must change", why)

    def test_allowed_again_after_14_days(self):
        override.grant(self.conn, CFG, "test", self.tid, check="lint", reason="first",
                       ttl="1h", fix_card=self.fix, issuer="fable", now=1000)
        after_14d = 1000 + override.REPEAT_WINDOW_S + 1
        ok, _ = override.grant(self.conn, CFG, "test", self.tid, check="lint", reason="second",
                               ttl="1h", fix_card=self.fix, issuer="fable", now=after_14d)
        self.assertTrue(ok)

    def test_different_boards_do_not_collide(self):
        override.grant(self.conn, CFG, "board-a", self.tid, check="lint", reason="first",
                       ttl="72h", fix_card=self.fix, issuer="fable", now=1000)
        ok, _ = override.grant(self.conn, CFG, "board-b", self.tid, check="lint", reason="second",
                               ttl="72h", fix_card=self.fix, issuer="fable", now=1001)
        self.assertTrue(ok)


class Report(OverrideCase):
    def test_totals_and_per_card_states(self):
        override.grant(self.conn, CFG, "test", self.tid, check="lint", reason="flaky",
                       ttl="1h", fix_card=self.fix, issuer="fable", now=1000)
        other = db.new_task(self.conn, title="other", body="", branch="epic/x")
        override.grant(self.conn, CFG, "test", other, check="unit-tests", reason="known gap",
                       ttl="1h", fix_card=self.fix, issuer="fable", now=1000)
        override.grant(self.conn, CFG, "test", self.tid, check="merge", reason="nope",
                       ttl="1h", fix_card=self.fix, issuer="fable", now=1000)   # refused: not operator
        now = 1000 + 7200   # past the 1h TTL: both grants are now expired
        doc = override.report(self.conn, "test", now=now)
        self.assertEqual(doc["totals"], {"granted": 2, "active": 0, "expired": 2, "refused": 1, "applied": 0})
        self.assertEqual([e["state"] for e in doc["by_task"][self.tid]], ["expired", "refused"])
        self.assertEqual([e["state"] for e in doc["by_task"][other]], ["expired"])


if __name__ == "__main__":
    unittest.main()
