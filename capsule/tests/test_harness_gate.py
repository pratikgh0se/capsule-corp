"""harness_gate.compare. Oracle 1: hand-computed fixtures. Oracle 2: properties."""
import random
import unittest

from capsule.harness_gate import compare


def run(**cases):
    cs = [{"case": k, "passed": v} for k, v in cases.items()]
    ok = sum(v for v in cases.values())
    return {"passed": ok, "failed": len(cs) - ok, "cases": cs}


class HandComputed(unittest.TestCase):
    def test_candidate_two_of_three_vs_baseline_three_of_three_is_worse(self):
        base = [run(a=True, b=True)] * 3
        cand = [run(a=True, b=True), run(a=True, b=False), run(a=True, b=True)]
        out = compare(base, cand)
        self.assertEqual(out["verdict"], "worse on b")
        self.assertEqual(out["per_case"]["b"], {"baseline": 1.0, "candidate": 2 / 3})
        self.assertEqual(out["per_case"]["a"], {"baseline": 1.0, "candidate": 1.0})
        self.assertEqual(out["baseline"], {"mean": 1.0, "spread": 0.0})
        self.assertAlmostEqual(out["candidate"]["mean"], 5 / 6)
        self.assertAlmostEqual(out["candidate"]["spread"], 0.5)

    def test_case_absent_in_one_arm_is_skipped_not_dropped(self):
        out = compare([run(a=True, b=True)], [run(a=True, c=True)])
        self.assertEqual(out["skipped"], ["b", "c"])
        self.assertEqual(list(out["per_case"]), ["a"])

    def test_identical_arms_no_change(self):
        arm = [run(a=True, b=False), run(a=False, b=False)]
        self.assertEqual(compare(arm, arm)["verdict"], "no change")

    def test_better_when_only_improvements(self):
        out = compare([run(a=False)], [run(a=True)])
        self.assertEqual(out["verdict"], "better on a")

    def test_empty_arms(self):
        out = compare([], [])
        self.assertEqual(out["verdict"], "unmeasured")
        self.assertEqual(out["baseline"], "unmeasured")

    def test_no_shared_cases_is_unmeasured(self):
        out = compare([run(a=True)], [run(b=True)])
        self.assertEqual(out["verdict"], "unmeasured")
        self.assertEqual(out["skipped"], ["a", "b"])

    def test_mixed_reports_both(self):
        out = compare([run(a=True, b=False)], [run(a=False, b=True)])
        self.assertEqual(out["verdict"], "worse on a; better on b")


class Properties(unittest.TestCase):
    def test_random_arms(self):
        rng = random.Random(7)
        names = list("abcde")
        for _ in range(200):
            def arm():
                return [run(**{n: rng.random() < 0.5 for n in names if rng.random() < 0.9})
                        for _ in range(rng.randint(1, 4))]
            base, cand = arm(), arm()
            out = compare(base, cand)
            self.assertEqual(compare(base, base)["verdict"], "no change")
            self.assertEqual(out["skipped"], compare(cand, base)["skipped"])
            for v in out["per_case"].values():
                self.assertTrue(0 <= v["baseline"] <= 1 and 0 <= v["candidate"] <= 1)
            swapped = compare(cand, base)["verdict"]
            if out["verdict"].startswith("worse"):
                self.assertNotEqual(swapped, "no change")
            if out["verdict"] in ("no change", "unmeasured"):
                self.assertEqual(swapped, out["verdict"])


if __name__ == "__main__":
    unittest.main()
