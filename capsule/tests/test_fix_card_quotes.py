"""inc_9a4137e9: a Korin fix card carries the text of each human file it names."""
import copy
import json
import os

from capsule import fix_card_quotes, incidents, paths, spool
from capsule.tests.test_korin import KorinCase, postmortem

H = fix_card_quotes.HEADING
TEXT = "Defer the golden rows.\nSecond line with `ticks` and ## a heading."


class QuoteHelpers:
    def setUp(self):
        super().setUp()
        self.done = spool.human_dir(self.home, "test") / "done"
        self.done.mkdir(parents=True)

    def put(self, name, text=TEXT, task_id="t_other", kind="answer", author="cos"):
        doc = {"task_id": task_id, "kind": kind, "text": text, "at": 5, "author": author}
        (self.done / name).write_text(json.dumps(doc))

    def report(self, body="Change capsule/tick.py.", **over):
        doc = postmortem("inc_x", **over)
        doc["fix_card"] = {"title": "fix", "body": body}
        return doc

    def quote(self, report):
        return fix_card_quotes.quoted(report, spool.human_dir(self.home, "test"))


class HumanFileQuotes(QuoteHelpers, KorinCase):
    def test_a_fix_card_naming_a_human_file_carries_its_text_verbatim(self):
        self.enable()
        self.put("7.json")
        root = "The builder could not read human/done/7.json, which belongs to another card."
        self.korin_docs.append(lambda inc: postmortem(inc, root_cause=root))
        self.crash_once(passes=5)
        rec = self.incidents()[0]
        self.assertEqual(rec["status"], "fix_pending")
        fix = self.conn.execute("SELECT body FROM tasks WHERE id=?", (rec["fix_card"],)).fetchone()
        self.assertIn(H, fix["body"])
        self.assertIn(TEXT, fix["body"])
        self.assertIn("task_id: t_other", fix["body"])
        self.assertIn("author: cos", fix["body"])

    def test_only_the_two_name_forms_match(self):
        self.put("7.json")
        for body, hit in (("see tests/7.json", False), ("see 7.json now", True),
                          ("see /tmp/x/human/done/7.json", True), ("a-7.json", False),
                          ("v7.json", False), ("x.7.json", False), ("human/done/7.json", True)):
            out = self.quote(self.report(body=body))
            self.assertEqual(TEXT in out["fix_card"]["body"], hit, body)

    def test_a_name_given_twice_is_one_entry_in_first_seen_order(self):
        self.put("7.json")
        self.put("3.json", text="third")
        out = self.quote(self.report(body="7.json then 3.json then human/done/7.json"))
        body = out["fix_card"]["body"]
        self.assertEqual(body.count(TEXT), 1)
        self.assertEqual(body.count(H), 1)
        self.assertLess(body.index("### 7.json"), body.index("### 3.json"))

    def test_a_missing_file_and_a_symlink_give_the_no_such_file_line(self):
        secret = self.home / "secret.json"
        secret.write_text(json.dumps({"task_id": "t", "kind": "answer", "text": "LEAKED"}))
        os.symlink(secret, self.done / "8.json")
        out = self.quote(self.report(body="8.json and 9.json"))
        body = out["fix_card"]["body"]
        self.assertIn("8.json: no such human file", body)
        self.assertIn("9.json: no such human file", body)
        self.assertNotIn("LEAKED", body)

    def test_a_missing_file_still_files_the_card(self):
        self.enable()
        self.korin_docs.append(lambda inc: postmortem(inc, mitigation="Quote 41.json in the card."))
        self.crash_once(passes=5)
        rec = self.incidents()[0]
        self.assertEqual(rec["status"], "fix_pending")
        q = "SELECT body FROM tasks WHERE id=?"
        body = self.conn.execute(q, (rec["fix_card"],)).fetchone()[0]
        self.assertIn("41.json: no such human file", body)

    def test_a_report_naming_no_human_file_gives_an_identical_body(self):
        doc = self.report()
        out = self.quote(doc)
        self.assertEqual(out["fix_card"]["body"], doc["fix_card"]["body"])
        self.assertEqual(incidents.fix_card_body({"id": "inc_x"}, out),
                         incidents.fix_card_body({"id": "inc_x"}, doc))

    def test_quoted_returns_a_copy_and_leaves_the_report_unchanged(self):
        self.put("7.json")
        doc = self.report(body="see 7.json")
        before = copy.deepcopy(doc)
        out = self.quote(doc)
        self.assertEqual(doc, before)
        self.assertIsNot(out, doc)
        self.assertNotEqual(out["fix_card"]["body"], doc["fix_card"]["body"])

    def test_a_huge_human_file_is_cut_with_a_marker_inside_the_schema_limit(self):
        self.put("7.json", text="x" * 50000)
        body = self.quote(self.report(body="see 7.json"))["fix_card"]["body"]
        self.assertLessEqual(len(body), fix_card_quotes.BODY_LIMIT)
        self.assertRegex(body, r"\[truncated: \d+ more chars\]$")
        self.assertIn("x" * 1000, body)

    def test_many_human_files_stop_at_the_total_cap_with_a_count_line(self):
        names = [f"{n}.json" for n in range(1, 16)]
        for name in names:
            self.put(name, text="y" * 1900)
        body = self.quote(self.report(body=" ".join(names)))["fix_card"]["body"]
        self.assertLessEqual(len(body), fix_card_quotes.BODY_LIMIT)
        self.assertRegex(body, r"\[\d+ more human files not quoted\]$")
        self.assertIn("### 1.json", body)
        self.assertNotIn("### 15.json", body)

    def test_small_human_files_stay_byte_identical(self):
        self.put("7.json")
        body = self.quote(self.report(body="see 7.json"))["fix_card"]["body"]
        self.assertEqual(body, "\n\n".join(["see 7.json", H, fix_card_quotes._entry(
            spool.human_dir(self.home, "test"), "7.json")]))
        self.assertTrue(body.endswith(TEXT))
        self.assertNotIn("truncated", body)


class QuoteRoom(QuoteHelpers, KorinCase):
    def test_the_body_limit_is_the_schema_max_length(self):
        schema = json.loads((paths.SCHEMAS / "postmortem.v1.json").read_text())
        want = schema["properties"]["fix_card"]["properties"]["body"]["maxLength"]
        self.assertEqual(fix_card_quotes.BODY_LIMIT, want)

    def test_a_long_card_body_never_exceeds_the_schema_limit(self):
        limit = fix_card_quotes.BODY_LIMIT
        self.put("7.json", text="z" * 3000)
        self.put("8.json", text="w" * 3000)
        for size in (limit - 2000, limit - 600, limit - 300, limit - 100, limit - 40, limit - 20,
                     limit - 5, limit):
            lead = "see 7.json 8.json "
            body = lead + "a" * (size - len(lead))
            out = self.quote(self.report(body=body))["fix_card"]["body"]
            self.assertLessEqual(len(out), limit, size)
            self.assertTrue(out.startswith(body), size)
        full = self.quote(self.report(body="see 7.json 8.json " + "a" * (limit - 250 - 18)))
        tail = full["fix_card"]["body"].rsplit("\n\n", 1)[-1]
        self.assertRegex(tail, r"^\[\d+ more human files not quoted\]$")
        self.assertNotIn(H, full["fix_card"]["body"])
        none = self.quote(self.report(body="see 7.json " + "a" * (limit - 11)))
        self.assertEqual(none["fix_card"]["body"], "see 7.json " + "a" * (limit - 11))
