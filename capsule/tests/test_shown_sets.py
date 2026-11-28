"""Few-shot shown sets (row 604, card t_c3415a6f).

Each role has `contracts/shown/<role>.jsonl`: 2-3 examples (good and bad) that `contracts.render`
puts in the role's system prompt. The held-out intent checks in `contracts/intent/*.jsonl` are the
test of the role, so no shown example may share an id with one or copy its given/expect text.
"""
import difflib
import hashlib
import json
import re
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from capsule import contracts, intent, paths

ROLES = ("implementer", "reviewer", "validator", "guide", "korin")
SHOWN = paths.REPO / "contracts" / "shown"
INTENT = paths.REPO / "contracts" / "intent"
WHOLE, PART = 0.6, 0.8   # similarity thresholds: given+expect together, or either alone


def _rows(directory):
    out = []
    for f in sorted(Path(directory).glob("*.jsonl")):
        out += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    return out


def _words(value):
    """Lower-case alphanumeric words of `value`, keys included, in `json.dumps` order."""
    if isinstance(value, dict):
        return [w for k, v in value.items() for w in _words(k) + _words(v)]
    if isinstance(value, (list, tuple)):
        return [w for v in value for w in _words(v)]
    return re.findall(r"[a-z0-9]+", str(value).lower())


def _reaches(a, b, limit):
    """Word-list similarity (0 to 1) is at least `limit`. Under 4 words on either side scores 0,
    so the 4 content-free held-out rows (a lone word such as "fail") copy undetected, by design.
    The similarity is the larger of the word-set overlap and the in-order match ratio; the slow
    ratio (building a matcher costs more than the bound) runs only when two cheap upper bounds
    on it, the length bound and the shared-word-count bound, both allow it."""
    if min(len(a), len(b)) < 4:
        return False
    if len(set(a) & set(b)) / len(set(a) | set(b)) >= limit:
        return True
    if 2 * min(len(a), len(b)) / (len(a) + len(b)) < limit:
        return False
    if 2 * sum((Counter(a) & Counter(b)).values()) / (len(a) + len(b)) < limit:
        return False
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= limit


def overlaps(shown, held_out):
    """One message per shown example that shares an id with, or nearly copies, a held-out one."""
    errors = []
    held_words = [(h, _words(h.get("given")), _words(h.get("expect"))) for h in held_out]
    for s in shown:
        sg, se = _words(s.get("given")), _words(s.get("expect"))
        sw = _words(s.get("why"))   # the prompt prints `why` too: a case pasted there leaks
        for h, hg, he in held_words:
            if s.get("id") == h.get("id"):
                errors.append(f"shown {s['id']!r} shares an id with a held-out example")
            elif any(_reaches(x, y, PART) for x, y in
                     ((sg + se + sw, hg + he), (sw, hg + he), (sw, hg), (sw, he), (sg, hg),
                      (se, he))) or _reaches(sg + se, hg + he, WHOLE):
                errors.append(f"shown {s['id']!r} near-duplicates held-out {h.get('id')!r}")
    return errors


class ShownFiles(unittest.TestCase):
    def test_each_role_has_two_or_three_good_and_bad_examples(self):
        for role in ROLES:
            rows = contracts._shown_examples(role)
            self.assertIn(len(rows), (2, 3), role)
            self.assertEqual({r["kind"] for r in rows}, {"good", "bad"}, role)

    def test_every_example_is_complete_and_sourced(self):
        for role in ROLES:
            ids = [r["id"] for r in contracts._shown_examples(role)]
            self.assertEqual(len(ids), len(set(ids)), role)
            for r in contracts._shown_examples(role):
                for key in ("id", "kind", "given", "expect", "why", "source"):
                    self.assertTrue(str(r.get(key, "")).strip(), (role, r.get("id"), key))
                self.assertTrue(intent._valid_source(r["source"]), (role, r["id"], r["source"]))
                self.assertNotIn("\n", r["why"])

    def test_bad_input_is_refused_loudly(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "x.jsonl").write_text('{"id": "a", "kind": "ugly"}\n')
            with self.assertRaises(ValueError):
                contracts._shown_examples("x", directory=Path(d))

    def test_shown_files_are_pinned(self):
        lines = (SHOWN / "SHA256SUMS").read_text().splitlines()
        sums = dict(reversed(line.split(None, 1)) for line in lines if line.strip())
        self.assertEqual(sorted(sums), sorted(f"{r}.jsonl" for r in ROLES))
        for name, digest in sums.items():
            actual = hashlib.sha256((SHOWN / name).read_bytes()).hexdigest()
            self.assertEqual(actual, digest.strip(), name)


class ShownSetsIntent(unittest.TestCase):
    def test_every_example_is_judged_by_the_real_loader(self):
        """Z1 oracle: each held-out row is run through `_shown_examples`; the verdict must match."""
        self.assertIn("shown_sets", intent.STANDALONE)
        self.assertEqual(intent.lint(INTENT / "shown_sets.jsonl", None, "shown_sets"), [])
        for ex in intent.load("shown_sets"):
            with tempfile.TemporaryDirectory() as d:
                (Path(d) / "x.jsonl").write_text(json.dumps(ex["given"]["document"]["row"]) + "\n")
                try:
                    contracts._shown_examples("x", directory=Path(d))
                    got = {"refused": False}
                except ValueError:
                    got = {"refused": True}
            self.assertEqual(intent._differs(ex["expect"], got, None), [], ex["id"])


class HeldOutNeverShown(unittest.TestCase):
    def test_no_shown_example_overlaps_a_held_out_one(self):
        shown, held = _rows(SHOWN), _rows(INTENT)
        self.assertGreater(len(held), 50)
        self.assertEqual(overlaps(shown, held), [])

    def test_a_copied_intent_example_is_caught(self):
        held = _rows(INTENT)
        copy = dict(held[0], kind="good", why="copied")
        self.assertTrue(overlaps([copy], held))   # same id and same text
        renamed = dict(copy, id="another-name")
        self.assertTrue(overlaps([renamed], held))   # same text under a new id
        reworded = dict(renamed, given=json.loads(json.dumps(renamed["given"])))
        reworded["given"]["document"]["extra"] = "word"
        self.assertTrue(overlaps([reworded], held))   # near copy

    def test_a_held_out_case_pasted_into_why_is_caught(self):
        held = _rows(INTENT)
        cleans = [{"id": f"zzz-{i}", "given": "the sky is blue today",   # held ids can repeat
                   "expect": "an umbrella stays shut",
                   "why":json.dumps(h["given"]) + " " + json.dumps(h["expect"])}
                  for i, h in enumerate(held)]
        caught = overlaps(cleans, held)   # one scan, each example judged against every held-out row
        missed = [h["id"] for h, c in zip(held, cleans)
                  if not any(e.startswith(f"shown {c['id']!r} ") for e in caught)]
        self.assertLessEqual(len(missed), 4, missed)   # only the content-free rows may slip

    def test_a_near_copy_only_the_in_order_ratio_catches_is_caught(self):
        """Few shared word kinds (set overlap 1/9), but 20 of 24 words in the same order."""
        held = [{"id": "h", "given": "x " * 20 + "t u v w", "expect": ""}]
        near = {"id": "s", "given": "x " * 20 + "p q r s", "expect": "", "why": ""}
        self.assertTrue(overlaps([near], held))
        self.assertEqual(overlaps([dict(near, given="x " * 5 + "p q r s")], held), [])

    def test_an_unrelated_example_passes(self):
        clean = {"id": "zzz", "given": "the sky is blue today", "expect": "an umbrella stays shut"}
        self.assertEqual(overlaps([clean], _rows(INTENT)), [])


class ShownInPrompt(unittest.TestCase):
    def test_each_roles_system_prompt_holds_its_shown_set(self):
        for role in ROLES:
            prompt = contracts.render(role)
            for r in contracts._shown_examples(role):
                for key in ("given", "expect", "why", "source"):
                    self.assertIn(str(r[key]), prompt, (role, r["id"], key))

    def test_no_system_prompt_holds_a_held_out_example(self):
        for role in ROLES:
            prompt = contracts.render(role)
            for h in _rows(INTENT):
                self.assertNotIn(h["id"], prompt, (role, h["id"]))
                self.assertNotIn(json.dumps(h["given"]), prompt, (role, h["id"]))

    def test_a_role_without_a_shown_file_renders_as_before(self):
        self.assertEqual(contracts._shown_examples("no-such-role"), [])


if __name__ == "__main__":
    unittest.main()
