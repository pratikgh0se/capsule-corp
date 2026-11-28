import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule.tests.public_tree import private_only
from capsule import vault_context as vc


def note(path, **kw):
    return {"path": path, "rank": kw.pop("rank", 1), "text": "fake", **kw}


def row(n, text="fake", **kw):
    return {"number": n, "rank": kw.pop("rank", 1), "text": text, **kw}


class GuardMore(unittest.TestCase):
    def reasons(self, withheld):
        return {w.get("path") or w["number"]: w["reason"] for w in withheld}

    def test_a_stale_row_and_note_each_carry_a_reason(self):
        _, withheld, _ = vc.guard([note("S.md", status="superseded"),
                                   row(4, status="parked")], [])
        self.assertEqual(set(self.reasons(withheld)), {"S.md", 4})
        self.assertTrue(all(self.reasons(withheld).values()))

    def test_crlf_superseded_note_is_withheld(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp).resolve()
            (vault / "Specs").mkdir()
            (vault / "Specs" / "n.md").write_bytes(b"---\r\nstatus: superseded\r\n---\r\nx")
            kept, withheld, _ = vc.guard([vc._note(vault, "Specs/n.md")], [])
        self.assertEqual((kept, len(withheld)), ([], 1))

    def test_a_later_row_outside_the_top_ten_takes_the_withheld_rows_place(self):
        aux = {**row(40, "Replaces row 9.", rank=99), "aux": True}
        kept, withheld, _ = vc.guard([row(9, rank=2), aux], [])
        placed = vc._place(kept, withheld)
        self.assertEqual([(k["number"], k["rank"], "aux" in k) for k in placed], [(40, 2, False)])
        self.assertEqual(vc._place([{**row(41), "aux": True}], withheld), [])


class ContradictionHeld(unittest.TestCase):
    def rec(self, **kw):
        return {"mode": "hybrid", "retrieved": "t", "flags": [], "kept": [], "withheld": [], **kw}

    def test_later_of_a_contradicts_pair_is_held_from_the_brief_and_counted(self):
        a = note("A.md", date="2026-09-01", contradicts=["B"], rank=1, agreed=True)
        b = note("B.md", date="2026-10-01", rank=2, agreed=True)
        kept, withheld, flags = vc.guard([a, b], [])
        self.assertEqual([k["path"] for k in kept], ["B.md"])   # the record keeps the later
        self.assertIn("contradiction", flags)
        text, final = vc._brief(self.rec(kept=kept, withheld=withheld, flags=flags))
        self.assertEqual(final["kept"], [])
        self.assertEqual({w["reason"] for w in final["withheld"]},
                         {"contradicted by B.md", "contradiction (under CoS review)"})
        self.assertIn("2 item(s) withheld", text)
        self.assertNotIn("[B.md]", text)

    def test_note_contradicting_a_cited_row_is_flagged_kept_and_held_live(self):
        for named in (["row 7"], ["[[Spec#row 7]]"], [7]):
            kept, _, flags = vc.guard([note("N.md", contradicts=named, agreed=True)], [7])
            self.assertEqual([k["path"] for k in kept], ["N.md"], named)
            self.assertIn("contradiction", flags, named)
            text, final = vc._brief(self.rec(kept=kept, flags=flags))
            self.assertEqual((final["kept"], "[N.md]" in text), ([], False), named)
            self.assertEqual(final["withheld"][0]["reason"], "contradiction (under CoS review)")
        _, _, other = vc.guard([note("N.md", contradicts=["row 8"])], [7])
        self.assertNotIn("contradiction", other)

    def test_either_side_may_declare_the_contradiction_in_any_link_form(self):
        forms = ("Knowledge/B.md", "Knowledge/B", "B", "[[B]]", "[[Knowledge/B]]", "[[B|alias]]")
        for form, declarer in [(f, d) for f in forms for d in "AB"]:
            with tempfile.TemporaryDirectory() as tmp:
                items = self.pair(Path(tmp).resolve(), form, declarer)
            kept, withheld, flags = vc.guard(items, [])
            tag = (form, declarer)
            self.assertEqual([k["path"] for k in kept], ["Knowledge/B.md"], tag)
            self.assertIn("contradiction", flags, tag)
            text, final = vc._brief(self.rec(kept=kept, withheld=withheld, flags=flags))
            self.assertEqual(final["kept"], [], tag)
            self.assertIn("2 item(s) withheld", text, tag)
            self.assertNotIn("[Knowledge/B.md]", text, tag)

    def pair(self, vault, form, declarer):
        (vault / "Knowledge").mkdir()
        for name, day, other in (("A", "2026-09-01", form), ("B", "2026-10-01", "A")):
            link = f"\nrelations:\n  contradicts: {other}" if name == declarer else ""
            (vault / "Knowledge" / f"{name}.md").write_text(
                f"---\ndate: {day}{link}\n---\n## For future agent\nbody {name}\n")
        return [{**vc._note(vault, f"Knowledge/{n}.md"), "rank": i, "agreed": True}
                for i, n in enumerate("AB", 1)]

    @private_only
    def test_the_pair_is_held_through_retrieve_too(self):
        engine = Path(__file__).parent / "fixtures" / "engine"
        for form in ("B", "[[Knowledge/B]]", "Knowledge/B.md", "[[B|alias]]"):
            with tempfile.TemporaryDirectory() as tmp:
                vault = Path(tmp).resolve()
                (vault / "Knowledge").mkdir()
                for name, day, link in (("A", "2026-09-01", f"relations:\n  contradicts: {form}\n"),
                                        ("B", "2026-10-01", "")):
                    (vault / "Knowledge" / f"{name}.md").write_text(
                        f"---\ndate: {day}\n{link}---\n## For future agent\nzorbulon {name}\n")
                cfg = {"vault_context": {"vault": str(vault), "skill_root": str(engine)}}
                with mock.patch.dict(os.environ, {"OLLAMA_URL": "http://127.0.0.1:9"}):
                    rec = vc.retrieve({"title": "zorbulon", "body": "zorbulon"}, cfg)
            text, final = vc._brief(rec)
            self.assertEqual(final["kept"], [], form)
            self.assertIn("2 item(s) withheld", text, form)

    def test_inline_relations_mapping_declares_the_contradiction(self):
        for decl in ("{contradicts: [[B]]}", "{contradicts: B}", "{contradicts: [B, C]}"):
            with tempfile.TemporaryDirectory() as tmp:
                vault = Path(tmp).resolve()
                (vault / "Specs").mkdir()
                (vault / "Specs" / "n.md").write_text(f"---\nrelations: {decl}\n---\nbody")
                item = vc._note(vault, "Specs/n.md")
            self.assertIn("b", item["contradicts"], decl)

    def test_frontmatter_row_reference_survives_into_the_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp).resolve()
            (vault / "Specs").mkdir()
            (vault / "Specs" / "n.md").write_text(
                "---\nrelations:\n  contradicts: [[Spec#row 7]]\n---\nbody")
            item = vc._note(vault, "Specs/n.md")
        self.assertIn(7, item["contradicts"])


class RetrieveMore(unittest.TestCase):
    def test_allow_given_as_a_string_is_one_folder(self):
        vault = Path(__file__).parent / "fixtures" / "vault"
        card = {"title": "Zorbulon flange cache", "body": "zorbulon flange retention and audit"}
        rec = vc.retrieve(card, {"vault_context": {"vault": str(vault), "allow": "Specs"}})
        self.assertTrue(all(k.get("number") or k["path"].startswith("Specs/") for k in rec["kept"]))


class RenderMore(unittest.TestCase):
    def test_row_bullet_shape(self):
        rec = {"mode": "hybrid", "retrieved": "t", "withheld": [],
               "kept": [row(601, "Row text.", date="2026-10-18", status="decided")]}
        self.assertIn("- [row 601] 2026-10-18, status decided: Row text.", vc.render(rec))

    def test_undated_note_has_no_none(self):
        rec = {"mode": "hybrid", "retrieved": "t", "withheld": [], "kept": [note("A.md")]}
        self.assertNotIn("None", vc.render(rec))


if __name__ == "__main__":
    unittest.main()
