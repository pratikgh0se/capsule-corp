import json, tempfile, unittest, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import heal_table

class HealTable(unittest.TestCase):
    def test_render(self):
        d = tempfile.mkdtemp(); p = pathlib.Path(d) / "heal.json"
        p.write_text(json.dumps({"docker": {"fails": 0, "first_fail": None}, "infisical": {"fails": 3, "first_fail": "2026-09-08T10:00:00+07:00"}}))
        out = heal_table.render(p)
        self.assertIn("docker      ok", out)
        self.assertIn("infisical   FAIL x3 since 2026-09-08 10:00", out)
    def test_missing_file(self):
        self.assertEqual(heal_table.render(pathlib.Path("/nonexistent/heal.json")), "heal: no state yet")
    def test_all_ok_flag(self):
        d = tempfile.mkdtemp(); p = pathlib.Path(d) / "heal.json"
        p.write_text(json.dumps({"docker": {"fails": 0, "first_fail": None}}))
        self.assertTrue(heal_table.all_ok(p))
        p.write_text(json.dumps({"docker": {"fails": 1, "first_fail": "2026-09-08T10:00:00+07:00"}}))
        self.assertFalse(heal_table.all_ok(p))

    def test_retired_component_in_live_file_is_ignored(self):
        d = tempfile.mkdtemp()
        p = pathlib.Path(d) / "heal.json"
        ok = {"fails": 0, "first_fail": None}
        stale = {"fails": 4, "first_fail": "2026-09-08T10:00:00+07:00"}
        p.write_text(json.dumps({"docker": ok, "gateway": stale}))
        self.assertNotIn("gateway", heal_table.render(p))
        self.assertTrue(heal_table.all_ok(p))
