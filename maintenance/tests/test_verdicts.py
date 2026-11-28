import json, os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import verdicts


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        # Restored, not leaked (L10): `test_refuses_the_default_dir_under_test`
        # deletes OPS_VERDICT_STATE, and without a cleanup that deletion outlived
        # this class and made module ordering load-bearing for every later test.
        for key, value in (("AGENT_OPS_NO_SEND", "1"), ("OPS_VERDICT_STATE", self.tmp.name)):
            self.addCleanup(self._restore, key, os.environ.get(key))
            os.environ[key] = value

    @staticmethod
    def _restore(key, old):
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old

    def rec(self, **kw):
        base = {"kind": "gate", "state": "pr_ready", "text": "gate: PR ready - x",
                "source": "merge_gate", "pr_url": None, "head": None, "mirrored": True}
        base.update(kw); return base

    def test_refuses_the_default_dir_under_test(self):
        del os.environ["OPS_VERDICT_STATE"]
        with self.assertRaises(RuntimeError):
            verdicts.verdict_dir()

    def test_modes_and_permissions(self):
        self.assertEqual(verdicts.mode({}), "off")
        self.assertEqual(verdicts.mode({"OPS_VERDICT_STORE": "shadow"}), "shadow")
        self.assertEqual(verdicts.mode({"OPS_VERDICT_STORE": "1"}), "on")
        self.assertEqual(verdicts.mode({"OPS_VERDICT_STORE": "nonsense"}), "off")
        p = verdicts.append("capsule-corp", "t_deadbeef", self.rec())
        self.assertEqual(oct(p.stat().st_mode)[-3:], "600")
        self.assertEqual(oct(p.parent.stat().st_mode)[-3:], "700")
        self.assertEqual(oct(p.parent.parent.stat().st_mode)[-3:], "700")

    def test_round_trip_newest_and_cap(self):
        for i in range(verdicts.RECORD_CAP + 5):
            verdicts.append("capsule-corp", "t_deadbeef", self.rec(state=f"s{i}"), now=1000.0 + i)
        verdicts.append("capsule-corp", "t_deadbeef", self.rec(kind="quality", state="fail", round=2), now=9999.0)
        doc = verdicts.read("capsule-corp", "t_deadbeef")
        self.assertEqual(doc["v"], 1)
        self.assertEqual(len(doc["records"]), verdicts.RECORD_CAP)
        self.assertEqual(verdicts.newest("capsule-corp", "t_deadbeef", kind="quality")["round"], 2)
        self.assertEqual(verdicts.newest("capsule-corp", "t_deadbeef", kind="gate")["state"], "s204")

    def test_bad_input_and_bad_file_read_as_absent(self):
        for bad in ("../etc", "a/b", "", "CAPSULE-CORP"):
            with self.assertRaises(ValueError):
                verdicts.verdict_path(bad, "t_deadbeef")
        with self.assertRaises(ValueError):
            verdicts.verdict_path("capsule-corp", "../../t_x")
        p = verdicts.verdict_path("capsule-corp", "t_cafe1234")
        p.parent.mkdir(parents=True, exist_ok=True); p.write_text("{ not json", encoding="utf-8")
        self.assertIsNone(verdicts.read("capsule-corp", "t_cafe1234"))
        p.write_text(json.dumps({"v": 99, "records": [self.rec()]}), encoding="utf-8")
        self.assertIsNone(verdicts.read("capsule-corp", "t_cafe1234"))
        self.assertIsNone(verdicts.newest("capsule-corp", "t_nothere"))

    def test_unknown_kind_is_refused_and_unknown_keys_survive(self):
        with self.assertRaises(ValueError):
            verdicts.append("capsule-corp", "t_deadbeef", self.rec(kind="approval"))
        verdicts.append("capsule-corp", "t_deadbeef", self.rec(future_key="x"))
        self.assertEqual(verdicts.newest("capsule-corp", "t_deadbeef")["future_key"], "x")


if __name__ == "__main__":
    unittest.main()
