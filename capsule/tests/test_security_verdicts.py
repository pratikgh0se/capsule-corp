import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from capsule import contracts, paths, security
from capsule.jsonschema import load_all
from capsule.__main__ import main

FID = "gitleaks:aws:a.py:3"


def _run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(list(argv))
    return rc, out.getvalue(), err.getvalue()


class SecurityVerdicts(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.repo = self.dir / "repo"
        (self.repo / "docs").mkdir(parents=True)

    def write(self, name, obj):
        p = self.dir / name
        p.write_text(obj if isinstance(obj, str) else json.dumps(obj))
        return str(p)

    def ledger(self, *lines):
        (self.repo / security.LEDGER).write_text(
            "".join((x if isinstance(x, str) else json.dumps(x)) + "\n" for x in lines))

    def check(self, findings, *extra):
        f = self.write("f.json", findings)
        return _run("security", "check", "--repo", str(self.repo), "--findings", f, *extra)

    def verdict(self, **kw):
        return dict({"id": FID, "verdict": "fix", "reason": "r", "date": "2026-11-08"}, **kw)

    def one(self):
        return [{"id": FID}]

    def test_contract_lints_and_carries_zenkai_line(self):
        path = paths.REPO / "contracts" / "security-engineer.md"
        self.assertEqual(contracts.lint(path, load_all(paths.SCHEMAS)), [])
        fields = contracts.parse(path)
        self.assertTrue(fields["may-not"].startswith("C;"))
        self.assertIn("edit code", fields["may-not"])
        self.assertIn("without a decision", fields["may-not"])
        self.assertIn("no network", fields["sees"])
        self.assertIn("Zenkai Rule", contracts.render("security-engineer"))

    def test_findings_normalise_both_tools_and_drop_secrets(self):
        g = self.write("g.json", [{"RuleID": "aws", "File": "a.py", "StartLine": 3,
                                   "Description": "key", "Secret": "S3CR3T", "Match": "M4TCH"}])
        s = self.write("s.json", {"results": [{"check_id": "x.y", "path": "b.py",
                                               "start": {"line": 9}, "extra": {"message": "m"}}]})
        rc, out, _ = _run("security", "findings", "--gitleaks", g, "--semgrep", s)
        self.assertEqual(rc, 0)
        self.assertNotIn("S3CR3T", out)
        self.assertNotIn("M4TCH", out)
        found = json.loads(out)
        self.assertEqual([f["id"] for f in found], [FID, "semgrep:x.y:b.py:9"])
        self.assertEqual(found[1]["summary"], "m")
        rc, out, _ = _run("security", "findings", "--semgrep", s)
        self.assertEqual(len(json.loads(out)), 1)

    def test_findings_bad_json_exits_2_naming_the_file(self):
        bad = self.write("bad.json", "{nope")
        rc, _, err = _run("security", "findings", "--gitleaks", bad)
        self.assertEqual(rc, 2)
        self.assertIn(bad, err)

    def test_missing_ledger_means_all_untriaged(self):
        rc, out, _ = self.check(self.one())
        self.assertEqual(rc, 0)
        self.assertIn(f"untriaged {FID}", out)
        self.assertIn("findings=1 triaged=0 untriaged=1 accepted=0", out)
        self.assertEqual(self.check(self.one(), "--strict")[0], 1)

    def test_triaged_ledger_is_clean_and_strict_passes(self):
        self.ledger(self.verdict(verdict="accept", decision="row 12"),
                    self.verdict(id="other:gone:z:1"))
        rc, out, _ = self.check(self.one(), "--strict")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "findings=1 triaged=1 untriaged=0 accepted=1")

    def test_ledger_problems_are_numbered_and_strict(self):
        self.ledger("not json", self.verdict(verdict="maybe"), self.verdict(reason=" "),
                    self.verdict(date="2026-13-40"), self.verdict(date="11/08/2026"),
                    self.verdict(verdict="accept"),
                    self.verdict(verdict="accept", decision="Row 4"),
                    self.verdict(verdict="accept", decision="D7"))
        rc, out, _ = self.check(self.one(), "--strict")
        self.assertEqual(rc, 1)
        for no in range(1, 8):
            self.assertIn(f"problem {no}: ", out)
        self.assertNotIn("problem 8", out)
        self.assertIn("triaged=1 untriaged=0 accepted=1", out)
        self.assertEqual(self.check(self.one())[0], 0)

    def test_accept_without_decision_is_not_triaged(self):
        self.ledger(self.verdict(verdict="accept"))
        rc, out, _ = self.check(self.one(), "--strict")
        self.assertEqual(rc, 1)
        self.assertIn(f"untriaged {FID}", out)
        self.assertIn("problem 1:", out)

    def test_findings_of_the_wrong_shape_exit_2_even_when_strict(self):
        for shape in ({"id": FID}, [{"rule": "x"}], ["gitleaks:a"], "text"):
            rc, _, err = self.check(shape, "--strict")
            self.assertEqual(rc, 2)
            self.assertIn("f.json", err)

    def test_check_writes_nothing_and_bad_findings_exit_2(self):
        self.ledger(self.verdict())
        before = sorted(p.name for p in self.dir.rglob("*"))
        self.check(self.one())
        self.assertEqual(before, sorted(p.name for p in self.dir.rglob("*") if p.name != "f.json"))
        bad = self.write("bad.json", "nope")
        rc, _, err = _run("security", "check", "--repo", str(self.repo), "--findings", bad)
        self.assertEqual((rc, bad in err), (2, True))

    def test_decision_regex_is_the_threat_models(self):
        from capsule import threat_model
        self.assertIs(security.DECISION, threat_model.DECISION)


if __name__ == "__main__":
    unittest.main()
