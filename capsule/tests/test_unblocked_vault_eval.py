"""A nightly vault-eval miss shows up as one `notice` item in `capsule unblocked`."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import unblocked, vault_eval


class VaultEvalNotice(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.home, self.state = tmp / "home", tmp / "state"
        (self.home / "boards" / "b").mkdir(parents=True)
        self.state.mkdir()   # the board has a config.json only, never a kanban.db
        mock.patch.object(vault_eval, "STATE", self.state).start()
        self.addCleanup(mock.patch.stopall)

    def _cfg(self, mode):
        (self.home / "boards" / "b" / "config.json").write_text(
            json.dumps({"redesign": {"vault_context": mode}}))

    def _result(self, day, failed=(), valid=True):
        (self.state / f"result-{day}.json").write_text(
            json.dumps({"date": day, "failed": list(failed), "valid": valid}))

    def _items(self):
        return unblocked._vault_eval_items(self.home)

    def test_miss_gives_one_notice_with_metrics_and_date(self):
        for mode in ("report", "live"):
            self._cfg(mode)
            self._result("2026-11-08", ["mrr", "stale_hits"])
            items = self._items()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].state, "notice")
            self.assertEqual(items[0].item, "vault-eval missed on 2026-11-08: mrr, stale_hits")
            self.assertFalse(items[0].unowned())
            self.assertEqual(unblocked.summarize(items), (0, 0))

    def test_counter_disagreement_is_a_miss(self):
        self._cfg("report")
        self._result("2026-11-08", valid=False)
        self.assertIn("counters disagree", self._items()[0].item)

    def test_newest_pass_after_older_failure_raises_nothing(self):
        self._cfg("report")
        self._result("2026-11-07", ["mrr"])
        self._result("2026-11-08")
        self.assertEqual(self._items(), [])

    def test_older_pass_then_newest_failure_notices(self):
        self._cfg("report")
        self._result("2026-11-07")
        self._result("2026-11-08", ["mrr"])
        self.assertIn("2026-11-08", self._items()[0].item)

    def test_wrong_shape_result_is_one_unreadable_notice(self):
        self._cfg("report")
        for raw in ("null", "[]", '{"failed": 3}', "{not json", '{"failed": null}'):
            (self.state / "result-2026-11-08.json").write_text(raw)
            self.assertEqual([i.item for i in self._items()],
                             ["vault-eval result unreadable on 2026-11-08"], raw)

    def test_string_failed_is_unreadable_and_odd_entries_are_stringified(self):
        self._cfg("report")
        (self.state / "result-2026-11-08.json").write_text('{"failed": "mrr"}')
        self.assertIn("unreadable", self._items()[0].item)
        (self.state / "result-2026-11-08.json").write_text('{"failed": [1, 2]}')
        self.assertEqual(self._items()[0].item, "vault-eval missed on 2026-11-08: 1, 2")

    def test_unreadable_result_with_switch_off_raises_nothing(self):
        self._cfg("off")
        (self.state / "result-2026-11-08.json").write_text("null")
        self.assertEqual(self._items(), [])

    def test_non_object_redesign_reads_as_off(self):
        (self.home / "boards" / "b" / "config.json").write_text('{"redesign": 5}')
        self._result("2026-11-08", ["mrr"])
        self.assertEqual(self._items(), [])
        self.assertEqual(vault_eval.mode(self.home), "off")

    def test_fresh_board_without_kanban_db_is_found_by_its_config(self):
        self.assertFalse(list((self.home / "boards").glob("*/kanban.db")))
        self._cfg("live")
        self.assertEqual(vault_eval.mode(self.home), "live")
        self._result("2026-11-08", ["mrr"])
        self.assertEqual(len(self._items()), 1)

    def test_kanban_db_only_board_falls_back_to_the_legacy_home_config(self):
        (self.home / "boards" / "b" / "kanban.db").touch()
        (self.home / "config.json").write_text('{"redesign": {"vault_context": "live"}}')
        self.assertEqual(vault_eval.mode(self.home), "live")
        self._result("2026-11-08", ["mrr"])
        self.assertEqual(len(self._items()), 1)

    def test_board_folder_without_any_config_is_off(self):
        (self.home / "boards" / "b" / "kanban.db").touch()
        self.assertEqual(vault_eval.mode(self.home), "off")

    def test_first_board_by_name_wins_and_no_board_is_off(self):
        (self.home / "boards" / "a").mkdir()
        (self.home / "boards" / "a" / "config.json").write_text(
            '{"redesign": {"vault_context": "report"}}')
        self._cfg("off")
        self.assertEqual(vault_eval.mode(self.home), "report")
        self.assertEqual(vault_eval.mode(self.home / "nowhere"), "off")

    def test_no_result_file_raises_nothing(self):
        self._cfg("report")
        self.assertEqual(self._items(), [])

    def test_switch_off_raises_nothing(self):
        self._cfg("off")
        self._result("2026-11-08", ["mrr"])
        self.assertEqual(self._items(), [])

    def test_no_config_raises_nothing(self):
        self._result("2026-11-08", ["mrr"])
        self.assertEqual(self._items(), [])

    def test_detect_includes_the_notice(self):
        self._cfg("report")
        self._result("2026-11-08", ["mrr"])
        found = [i for i in unblocked.detect(capsule_home=self.home, now=1,
                                            git_run=mock.Mock(side_effect=OSError))
                 if i.source == "vault-eval"]
        self.assertEqual(len(found), 1)


if __name__ == "__main__":
    unittest.main()
