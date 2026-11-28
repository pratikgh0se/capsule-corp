# mutation-check: targets-predate-range b957739c9 -- rename only, no behaviour pinned ::
#   UsageGuardPauseRemoved.test_the_pause_path_is_gone,
#   UsageGuardPauseRemoved.test_usage_window_reads_no_legacy_state
import os, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["AGENT_OPS_NO_SEND"] = "1"
import usage_guard as ug
from capsule.tests.private_names import retired_names, retired_only, retired_word  # noqa: E402


class UsageGuardPauseRemoved(unittest.TestCase):
    @retired_only
    def test_the_pause_path_is_gone(self):
        for name in ("decide", retired_word(), "save_guard", "unblock_limit_victims",
                     "recent_limit_failures", "week_pause_at", "STAMP",
                     retired_word() + "_present", "AUTOMATION_HOME"):
            self.assertFalse(hasattr(ug, name), name)
        for word in retired_names():
            self.assertEqual([n for n in dir(ug) if word in n.lower()], [])

    @retired_only
    def test_usage_window_reads_no_legacy_state(self):
        import inspect
        src = inspect.getsource(ug.usage_window)
        for word in (retired_word(), retired_word().upper(), "account_usage"):
            self.assertNotIn(word, src)


if __name__ == "__main__":
    unittest.main()
