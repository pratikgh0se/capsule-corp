# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
#   Card.test_card_names_what_the_harness_should_have_done,
#   Classify.test_declared_product_that_skips_a_check_is_harness_and_flags
"""Factory autonomy Task 5 (spec 3.2, 3.3): which human touches are defects. Oracle 1: the
decision's declared scope. Oracle 2: the lexical reading of its options (Z2)."""
import json
import unittest

from capsule import intent, paths, touch

REVERT = {"question": "plan-doc-edit-outside-file-set", "recommended": "revert",
          "options": [{"id": "revert", "label": "revert the plan hunk off the epic branch"},
                      {"id": "keep", "label": "keep the hunk and widen the file set"}]}
PRODUCT_HOLD = {"status": "blocked", "block_kind": "needs_input", "hold": "which platform ships first"}
PRODUCT = {"question": "which first", "recommended": "ios", "scope": "product",
           "options": [{"id": "ios", "label": "ship the iOS app first"}, {"id": "web", "label": "ship the website first"}]}


class ReasonClass(unittest.TestCase):
    def test_table(self):
        rc = touch.reason_class
        self.assertEqual(rc({"status": "blocked", "block_kind": "needs_input", "hold": "alignment drifted (rows 1)"}),
                         "needs_input/guide-drift")
        self.assertEqual(rc({"status": "triage", "block_kind": "needs_input", "hold": "left undone / issues: x"}),
                         "needs_input/impl-hold")
        self.assertEqual(rc({"status": "blocked", "block_kind": "needs_input", "hold": "Korin fix card for inc_1"}),
                         "needs_input/korin-fix")
        self.assertEqual(rc({"status": "blocked", "block_kind": "needs_input", "hold": "why"}), "needs_input/other")
        self.assertEqual(rc({"status": "triage", "block_kind": None, "hold": "triage (failure 3/3)"}), "failure-limit")
        self.assertEqual(rc({"status": "blocked", "block_kind": "spec_stale", "hold": ""}), "spec_stale")
        self.assertEqual(rc({"status": "done", "block_kind": None, "hold": ""}), "done")
        self.assertEqual(rc(None), "unknown")


class Classify(unittest.TestCase):
    def test_non_answer_verbs(self):
        self.assertEqual(touch.classify("unblock", None), ("harness", False))
        self.assertEqual(touch.classify("screen-ok", None), ("product", False))

    def test_answers(self):
        self.assertEqual(touch.classify("answer", REVERT), ("text", False))
        self.assertEqual(touch.classify("answer", PRODUCT, PRODUCT_HOLD), ("product", False))
        self.assertEqual(touch.classify("answer", dict(PRODUCT, scope="harness")), ("harness", False))
        self.assertEqual(touch.classify("answer", None), ("harness", False))
        undeclared = {k: v for k, v in PRODUCT.items() if k != "scope"}
        self.assertEqual(touch.classify("answer", undeclared), ("text", False))    # unknown scope: not product

    def test_declared_product_with_text_mechanics_is_text_and_flags(self):
        self.assertEqual(touch.classify("answer", dict(REVERT, scope="product")), ("text", True))

    def test_declared_product_that_skips_a_check_is_harness_and_flags(self):
        """Adversarial (source owner): the stopping agent labels a gate bypass a product choice."""
        skip = {"question": "ship it?", "recommended": "go", "scope": "product",
                "options": [{"id": "go", "label": "skip the failing check and merge"}, {"id": "hold", "label": "wait"}]}
        self.assertEqual(touch.classify("answer", skip, PRODUCT_HOLD), ("harness", True))

    def test_the_tick_written_hold_outranks_a_declared_product_scope(self):
        """Oracle 3 (independent of the decision document): a card held on a harness reason at touch time
        was never waiting on a product question."""
        drift = {"status": "blocked", "block_kind": "needs_input", "hold": "alignment drifted (rows 452): x"}
        self.assertEqual(touch.classify("answer", PRODUCT, drift), ("harness", True))
        self.assertEqual(touch.classify("answer", PRODUCT, PRODUCT_HOLD), ("product", False))

    def test_an_amendment_on_any_option_is_text(self):
        doc = dict(PRODUCT, options=[dict(PRODUCT["options"][0], amendment=[{"target": "card", "find": "a",
                                                                               "replace": "b"}]),
                                     PRODUCT["options"][1]])
        self.assertEqual(touch.lexical(doc), "text")


class Card(unittest.TestCase):
    def test_card_names_what_the_harness_should_have_done(self):
        title, body = touch.card("sample-app", "t_de1466d6", "unblock", "needs_input/impl-hold",
                                 "go on", "inc_0000abcd")
        self.assertTrue(title.startswith("Harness should have: "))
        self.assertLessEqual(len(title), 120)
        for part in ("sample-app", "t_de1466d6", "unblock", "needs_input/impl-hold",
                     "inc_0000abcd", "Z6"):
            self.assertIn(part, body)
        self.assertIn("row 485", touch.should_have("unblock", "needs_input/impl-hold"))
        self.assertIn("unblock", touch.should_have("unblock", "zzz"))                # the default row


class IntentExamples(unittest.TestCase):
    def test_every_example_matches_its_oracle(self):
        lines = [json.loads(x) for x in (paths.INTENT / "touch.jsonl").read_text().splitlines() if x.strip()]
        self.assertEqual(len(lines), 4)
        for ex in lines:
            doc = ex["given"]["document"]
            decision = doc.get("decision") if isinstance(doc.get("decision"), dict) else None
            cls, _ = touch.classify(doc["verb"], decision, doc.get("was"))
            got = {"class": cls, "key": touch.key(doc["verb"], touch.reason_class(doc.get("was")))}
            self.assertTrue(intent.matches(ex["expect"], got), (ex["id"], got))


class ReasonClassSpecTable(unittest.TestCase):
    """Fix round 1, finding 1: a literal copy of spec 3.2's table, independent of the code's own
    grouping in `reason_class`, so a mutant that mis-routes any single row is caught here even
    though the two literals agree today."""
    ROWS = (
        ({"status": "blocked", "block_kind": "needs_input", "hold": "alignment drifted (rows 1)"},
         "needs_input/guide-drift"),
        ({"status": "triage", "block_kind": "needs_input", "hold": "left undone / issues: x"},
         "needs_input/impl-hold"),
        ({"status": "blocked", "block_kind": "needs_input", "hold": "Korin fix card for inc_1"},
         "needs_input/korin-fix"),
        ({"status": "blocked", "block_kind": "needs_input", "hold": "why"}, "needs_input/other"),
        ({"status": "triage", "block_kind": None, "hold": "triage (failure 3/3)"}, "failure-limit"),
        ({"status": "triage", "block_kind": None, "hold": "anything at all"}, "failure-limit"),
        ({"status": "blocked", "block_kind": "spec_stale", "hold": ""}, "spec_stale"),
        ({"status": "blocked", "block_kind": "review_infra", "hold": ""}, "review_infra"),
        ({"status": "blocked", "block_kind": "guide_ruling", "hold": ""}, "guide_ruling"),
        ({"status": "blocked", "block_kind": "parked", "hold": ""}, "parked"),
        ({"status": "blocked", "block_kind": "contradiction_loop", "hold": ""}, "contradiction_loop"),
        ({"status": "done", "block_kind": None, "hold": ""}, "done"),
        ({"status": "review", "block_kind": None, "hold": ""}, "review"),
        ({"status": "ready", "block_kind": None, "hold": ""}, "ready"),
        (None, "unknown"),
        ({}, "unknown"),
    )

    def test_every_row(self):
        for was, expected in self.ROWS:
            with self.subTest(was=was):
                self.assertEqual(touch.reason_class(was), expected)


class HarnessHoldsCoverage(unittest.TestCase):
    """Fix round 1, finding 1: oracle 2, a literal copy of spec 3.3's `HARNESS_HOLDS` list.
    Pins each of the 9 entries individually against a declared-`product` decision, so dropping
    any single one from `touch.HARNESS_HOLDS` still fails here (mutant: remove one hold)."""
    SPEC_HARNESS_HOLDS = ("review_infra", "spec_stale", "failure-limit", "needs_input/guide-drift",
                          "needs_input/impl-hold", "needs_input/korin-fix", "parked",
                          "contradiction_loop", "guide_ruling")
    WAS_FOR = {
        "review_infra": {"status": "blocked", "block_kind": "review_infra", "hold": ""},
        "spec_stale": {"status": "blocked", "block_kind": "spec_stale", "hold": ""},
        "failure-limit": {"status": "triage", "block_kind": None, "hold": "triage (failure 3/3)"},
        "needs_input/guide-drift": {"status": "blocked", "block_kind": "needs_input",
                                     "hold": "alignment drifted (rows 1)"},
        "needs_input/impl-hold": {"status": "triage", "block_kind": "needs_input",
                                   "hold": "left undone / issues: x"},
        "needs_input/korin-fix": {"status": "blocked", "block_kind": "needs_input",
                                   "hold": "Korin fix card for inc_1"},
        "parked": {"status": "blocked", "block_kind": "parked", "hold": ""},
        "contradiction_loop": {"status": "blocked", "block_kind": "contradiction_loop", "hold": ""},
        "guide_ruling": {"status": "blocked", "block_kind": "guide_ruling", "hold": ""},
    }

    def test_touch_HARNESS_HOLDS_matches_the_spec_list(self):
        self.assertEqual(set(touch.HARNESS_HOLDS), set(self.SPEC_HARNESS_HOLDS))
        self.assertEqual(len(touch.HARNESS_HOLDS), 9)

    def test_each_hold_individually_forces_harness_on_a_declared_product(self):
        for label in self.SPEC_HARNESS_HOLDS:
            with self.subTest(hold=label):
                self.assertEqual(touch.classify("answer", PRODUCT, self.WAS_FOR[label]), ("harness", True))


class HarnessMechanicsWords(unittest.TestCase):
    """Fix round 1, finding 1: each `HARNESS_MECHANICS` word pinned alone (the adversarial
    example in touch.jsonl carries both `skip` and `merge`, so it can't tell them apart), plus
    `re.I` (mutant: drop the flag)."""
    WORDS = ("retry", "rerun", "re-run", "reset", "unblock", "skip", "override", "merge", "accept",
             "reroute", "restart", "dispatch", "park")

    def _decision(self, label):
        return {"question": "ship it?", "recommended": "go", "scope": "product",
                "options": [{"id": "go", "label": label}, {"id": "hold", "label": "wait"}]}

    def test_each_word_individually_forces_harness(self):
        for word in self.WORDS:
            with self.subTest(word=word):
                decision = self._decision(f"{word} the pipeline now")
                self.assertEqual(touch.classify("answer", decision, PRODUCT_HOLD), ("harness", True))

    def test_case_insensitive(self):
        decision = self._decision("MERGE the branch now")
        self.assertEqual(touch.classify("answer", decision, PRODUCT_HOLD), ("harness", True))


class Row512OracleMissing(unittest.TestCase):
    """Row 512 (coordinator ruling, fix round 1 finding 2): the independent oracle - a tick-
    written `was` snapshot that resolves to a known reason, or a `recommended` id that matches
    one of the document's own options - must be present before a declared `product` scope is
    honoured on its own. Missing or empty `was`, or a `recommended` matching no option, is
    `harness` plus a disagreement flag, never `product` on the agent's declaration alone."""
    def test_missing_was_is_harness_with_disagreement(self):
        self.assertEqual(touch.classify("answer", PRODUCT), ("harness", True))

    def test_empty_was_is_harness_with_disagreement(self):
        self.assertEqual(touch.classify("answer", PRODUCT, {}), ("harness", True))

    def test_recommended_matching_no_option_is_harness_with_disagreement(self):
        stray = dict(PRODUCT, recommended="zzz")
        self.assertEqual(touch.classify("answer", stray, PRODUCT_HOLD), ("harness", True))


if __name__ == "__main__":
    unittest.main()
