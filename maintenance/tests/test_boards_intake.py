# mutation-check: targets-predate-range b957739c9 -- rename only, no behaviour pinned ::
#   BoardsCliWithoutLegacy.test_legacy_boards_join_only_when_legacy_is_present
"""The security job's board list: Capsule boards always, legacy-store boards only when present."""
import contextlib, io, os, shutil, sys, tempfile, unittest, unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("AGENT_OPS_NO_SEND", "1")
import board, board_bridge as bb, intake  # noqa: E402


class _CapsuleAndLegacyBoards(unittest.TestCase):
    def setUp(self):
        self.capsule, self.legacy_home = (Path(tempfile.mkdtemp(prefix="bcw-")) for _ in range(2))
        cap, old = self.capsule / "boards", self.legacy_home / "kanban" / "boards"
        for root, n in ((cap, "capsule-corp"), (cap, "sample-app"), (old, "legacy")):
            (root / n).mkdir(parents=True)
            (root / n / "kanban.db").write_text("")
        for patcher in (unittest.mock.patch.dict(os.environ, CAPSULE_HOME=str(self.capsule)),
                        unittest.mock.patch.object(board, "BOARDS_DIR", old)):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("LEGACY_PRESENT", None)


class BoardsCliWithoutLegacy(_CapsuleAndLegacyBoards):
    def _boards(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            bb.main(["boards"])
        self.assertEqual(cm.exception.code, 0)
        return out.getvalue().strip()

    def test_boards_cli_lists_capsule_boards_when_legacy_is_absent(self):
        shutil.rmtree(self.legacy_home / "kanban" / "boards" / "legacy")
        self.assertEqual(self._boards(), "capsule-corp sample-app")

    def test_legacy_boards_join_only_when_legacy_is_present(self):
        self.assertNotIn("legacy", self._boards().split())
        os.environ["LEGACY_PRESENT"] = "1"
        self.assertEqual(self._boards(), "capsule-corp legacy sample-app")

    def test_legacy_boards_join_with_the_new_switch_name_too(self):
        os.environ["LEGACY_PRESENT"] = "1"
        self.assertEqual(self._boards(), "capsule-corp legacy sample-app")


class AddFindingBoardNames(_CapsuleAndLegacyBoards):
    def test_add_finding_accepts_capsule_board_and_refuses_unknown(self):
        vault = Path(tempfile.mkdtemp(prefix="af-vault-"))
        (vault / "Boards").mkdir()
        (vault / "Boards" / "Intake.md").write_text(f"---\ntype: intake\n---\n{intake.FINDINGS}\n")
        _, appended = intake.add_finding("capsule-corp", "med", "T", "sec 1", vault=vault)
        self.assertTrue(appended)
        for name in ("nope", "legacy"):
            with self.assertRaises(ValueError):
                intake.add_finding(name, "med", "T", "sec 1", vault=vault)


if __name__ == "__main__":
    unittest.main()
