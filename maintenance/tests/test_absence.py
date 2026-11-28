import json, os, sys, time, unittest, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import absence  # noqa: E402


class Predicate(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        os.environ["MERGE_GATE_STATE"] = str(self.d / "merge-gate")
        os.environ["AGENT_OPS_STATE_DIR"] = str(self.d / "state")
        os.environ["OPS_ABSENCE_PROTOCOL"] = "1"
        (self.d / "state").mkdir()

    def _stamp(self, rel, age_s):
        p = Path(os.environ["MERGE_GATE_STATE"]) / "approvals" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"ts": 1}', encoding="utf-8")
        t = time.time() - age_s
        os.utime(p, (t, t))
        return p

    def test_no_approvals_dir_is_tier_1_not_tier_3(self):
        # a never-approved system is a new one, not an abandoned one
        self.assertIsNone(absence.last_human_ts())
        self.assertEqual(absence.tier(), absence.CONTINUE)

    def test_a_pr_stamp_and_a_spec_stamp_both_count(self):
        self._stamp("capsule-corp/t_abc123.json", 10 * 3600)
        self.assertEqual(absence.tier(), absence.CONTINUE)
        self._stamp("spec/some-slug.json", 5 * 3600)
        self.assertAlmostEqual(absence.state()["age_h"], 5, delta=0.2)

    def test_the_three_boundaries(self):
        for age_h, want in ((47, absence.CONTINUE), (49, absence.DRAIN),
                            (8 * 24, absence.PARK)):
            with self.subTest(age_h=age_h):
                p = self._stamp("capsule-corp/t_abc123.json", age_h * 3600)
                self.assertEqual(absence.tier(), want)
                p.unlink()

    def test_human_seen_resets_the_clock(self):
        self._stamp("capsule-corp/t_abc123.json", 9 * 24 * 3600)
        self.assertEqual(absence.tier(), absence.PARK)
        absence.seen()
        self.assertEqual(absence.tier(), absence.CONTINUE)

    def test_a_held_tier_wins_both_ways(self):
        self._stamp("capsule-corp/t_abc123.json", 1 * 3600)
        absence.hold(3)
        self.assertEqual(absence.tier(), absence.PARK)
        self.assertEqual(absence.state()["held"], 3)
        absence.clear()
        self.assertEqual(absence.tier(), absence.CONTINUE)

    def test_a_malformed_state_file_reads_as_absent(self):
        (Path(os.environ["AGENT_OPS_STATE_DIR"]) / "absence.json").write_text("{{{", encoding="utf-8")
        self._stamp("capsule-corp/t_abc123.json", 9 * 24 * 3600)
        self.assertEqual(absence.tier(), absence.PARK)   # not a crash, not tier 1-by-accident
        (Path(os.environ["AGENT_OPS_STATE_DIR"]) / "absence.json").write_text(
            json.dumps({"v": 99, "held": 2}), encoding="utf-8")
        self.assertEqual(absence.tier(), absence.PARK)   # unknown v is ignored

    def test_refuses_the_default_dirs_under_test(self):
        # Same override-or-refuse rule as verdicts.verdict_dir() and
        # merge_gate.state_dir(): under AGENT_OPS_NO_SEND with no override,
        # falling through to the real ~/.local/state / maintenance/state
        # path is always a bug, never intended.
        os.environ["AGENT_OPS_NO_SEND"] = "1"
        del os.environ["MERGE_GATE_STATE"]
        del os.environ["AGENT_OPS_STATE_DIR"]
        with self.assertRaises(RuntimeError):
            absence.last_human_ts()
        with self.assertRaises(RuntimeError):
            absence.state()


class Flag(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        os.environ["MERGE_GATE_STATE"] = str(self.d / "merge-gate")
        os.environ["AGENT_OPS_STATE_DIR"] = str(self.d / "state")
        (self.d / "state").mkdir()

    def test_flag_off_is_always_tier_1_and_never_blocks(self):
        for raw in ("0", "", "nope", "off"):
            with self.subTest(raw=raw):
                os.environ["OPS_ABSENCE_PROTOCOL"] = raw
                self.assertFalse(absence.enabled())
                self.assertEqual(absence.tier(), absence.CONTINUE)
                self.assertIsNone(absence.block_reason())

    def test_flag_on_spellings(self):
        for raw in ("1", "true", "yes", "ON"):
            with self.subTest(raw=raw):
                os.environ["OPS_ABSENCE_PROTOCOL"] = raw
                self.assertTrue(absence.enabled())

    def test_block_reason_is_none_on_tier_1_and_a_sentence_otherwise(self):
        os.environ["OPS_ABSENCE_PROTOCOL"] = "1"
        self.assertIsNone(absence.block_reason())
        absence.hold(2)
        self.assertIn("absence", absence.block_reason())
        self.assertIn("drain", absence.block_reason())
