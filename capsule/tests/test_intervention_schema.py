"""Row 569 item 2: `contracts/schemas/rows/intervention.v1.json`, validated by `human.write`.
Oracle 1: the real writer's output (and the real CLI-shaped calls) validate. Oracle 2: hand-written
bad files are rejected by the schema, and the writer's own hand checks (`OVERRIDE_FIELDS`, the
park guard, `TAGS`) still fire on their own - the schema is a second check beside them, never a
replacement."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from capsule import human, intent, row_schemas

GOOD = {"task_id": "t_0123abcd", "kind": "unblock", "text": "answered the pause question", "at": 1790000000}


class InterventionSchema(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)

    def written(self, kind, text, **kw):
        path = human.write(self.home, "b", "t_0123abcd", kind, text, now=5, **kw)
        return json.loads(path.read_text())

    def test_real_writer_output_validates_for_every_verb_the_tick_acts_on(self):
        docs = [self.written("reset", "retry", tag="migration-reset"), self.written("unblock", "go"),
                self.written("accept", "ok"), self.written("screen-ok", "looks right"),
                self.written("screen-fix", "fix the header"), self.written("park", "waiting on a vendor"),
                self.written("epic", "epic/screens"), self.written("nudge", ""),
                self.written("override", "reason", check="intent-example", ttl="72h", fix_card="t_89abcdef",
                             issuer="owner")]
        self.assertEqual(len(docs), 9)
        for doc in docs:
            with self.subTest(kind=doc["kind"]):
                self.assertEqual(row_schemas.problems("intervention.v1", doc), [])

    def test_hand_written_bad_files_are_rejected_by_name(self):
        bad = {
            "empty task id": dict(GOOD, task_id=""),
            "task id with a space": dict(GOOD, task_id="t_ x"),
            "missing at": {k: v for k, v in GOOD.items() if k != "at"},
            "at is a string": dict(GOOD, at="1790000000"),
            "negative at": dict(GOOD, at=-1),
            "kind in capitals": dict(GOOD, kind="Unblock"),
            "empty kind": dict(GOOD, kind=""),
            "text is a list": dict(GOOD, text=["go"]),
            "unknown tag": dict(GOOD, tag="forced"),
            "unknown field": dict(GOOD, severity="high"),
            "empty issuer": dict(GOOD, issuer=""),
        }
        for label, doc in bad.items():
            with self.subTest(label):
                self.assertTrue(row_schemas.problems("intervention.v1", doc), label)

    def test_a_valid_hand_written_file_with_an_optional_field_is_accepted(self):
        self.assertEqual(row_schemas.problems("intervention.v1", dict(GOOD, tag="migration-reset")), [])

    def test_a_patrol_entry_is_checked_against_the_schema_before_it_lands(self):
        from capsule import patrol
        act = {"card": "t_0123abcd", "verb": "rerun", "text": "x", "role": "r" * 50}
        with self.assertRaises(ValueError):
            patrol._human(self.home, "b", act, 5)
        patrol._human(self.home, "b", dict(act, role="reviewer"), 5)
        files = list(self.home.rglob("*.json"))
        self.assertEqual(len(files), 1)
        got = json.loads(files[0].read_text())
        self.assertEqual((got["author"], got["tag"], got["role"]),
                         ("capsule", "patrol", "reviewer"))
        self.assertEqual(row_schemas.problems("intervention.v1", got), [])

    def test_the_schema_tag_enum_is_the_writers_tags_and_the_patrol(self):
        schema = row_schemas.load("intervention.v1")
        self.assertEqual(tuple(schema["properties"]["tag"]["enum"]), (*human.TAGS, "patrol"))

    def test_the_schema_properties_cover_every_field_the_writer_can_emit(self):
        schema = row_schemas.load("intervention.v1")
        emitted = set(self.written("override", "r", tag="migration-reset", check="c", ttl="1h", fix_card="t_89abcdef",
                                   issuer="p"))
        emitted |= set(self.written("epic", "epic/a", tag="handover-route", assignee="dev-backend"))
        emitted |= set(self.written("unblock", "go", via="slack", ask="0123456789abcdef"))
        self.assertEqual(emitted, set(schema["properties"]) - {"role"})   # the patrol only

    def test_the_writer_refuses_a_bad_task_id_before_writing_a_file(self):
        with self.assertRaisesRegex(ValueError, "intervention.v1"):
            human.write(self.home, "b", "task one", "unblock", "go", now=1)
        self.assertEqual(list((self.home / "boards" / "b" / "human").glob("*.json")) if (self.home / "boards").exists()
                         else [], [])

    def test_the_writers_own_hand_checks_still_fire_beside_the_schema(self):
        with self.assertRaisesRegex(ValueError, "override needs check, ttl, fix_card, issuer"):
            human.write(self.home, "b", "t_0123abcd", "override", "why", now=1)
        with self.assertRaisesRegex(ValueError, "park needs a non-empty trigger"):
            human.write(self.home, "b", "t_0123abcd", "park", " ", now=1)
        with self.assertRaisesRegex(ValueError, "unknown intervention tag"):
            human.write(self.home, "b", "t_0123abcd", "reset", "x", now=1, tag="forced")


class IntentExamples(unittest.TestCase):
    """Z1: `contracts/intent/intervention_schema.jsonl`, each example run through the real schema."""

    def test_every_example_matches_what_the_schema_does(self):
        examples = intent.load("intervention_schema")
        self.assertTrue(1 <= len(examples) <= 5)   # the 3-new-per-round cap is test_intent's
        for ex in examples:
            row = ex["given"]["document"]["row"]
            self.assertEqual(row_schemas.problems("intervention.v1", row) == [], ex["expect"]["valid"], ex["id"])


class SpecificViolations(unittest.TestCase):
    """Fix round 1 (review F6): a rejection names the violated field, not just 'something'."""

    def test_the_message_names_the_field_and_the_rule(self):
        self.assertEqual(row_schemas.problems("intervention.v1", dict(GOOD, tag="forced")),
                         ["#/tag: must be one of ['migration-reset', 'handover-route', 'patrol']"])
        self.assertEqual(row_schemas.problems("intervention.v1", {k: v for k, v in GOOD.items() if k != "at"}),
                         ["#: missing required field 'at'"])
