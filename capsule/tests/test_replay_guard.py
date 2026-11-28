import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import db, replay


class LiveBoardGuard(unittest.TestCase):
    def _live(self):
        home = Path(tempfile.mkdtemp()).resolve()
        live = home / "boards" / "x" / "kanban.db"
        live.parent.mkdir(parents=True)
        db.create_board(live)
        return home, live

    def test_a_live_board_is_refused_through_a_symlink_a_hard_link_and_the_configured_home(self):
        home, live = self._live()
        with self.assertRaises(ValueError):
            replay._refuse_live(live, home=home)
        replay._refuse_live(home / "copy.db", home=home)     # a copy outside boards/ is fine
        other = Path(tempfile.mkdtemp())
        (other / "dir").symlink_to(live.parent)
        (other / "file.db").symlink_to(live)
        os.link(live, other / "hard.db")
        copy = other / "copy.db"
        copy.write_bytes(live.read_bytes())
        dotted = home / "boards" / ".." / "boards" / "x" / "kanban.db"
        with mock.patch.dict(os.environ, {"CAPSULE_HOME": str(home)}):    # no `home=` argument
            for path in (other / "dir" / "kanban.db", other / "file.db", other / "hard.db", live,
                         dotted):
                with self.assertRaises(ValueError, msg=str(path)):
                    replay._refuse_live(path)
            with self.assertRaises(ValueError):
                replay.snapshot(live)
            replay._refuse_live(copy)

    def test_a_live_board_under_the_default_home_is_refused_when_capsule_home_is_elsewhere(self):
        fake_user = Path(tempfile.mkdtemp()).resolve()
        live = fake_user / ".local" / "share" / "capsule" / "boards" / "x" / "kanban.db"
        live.parent.mkdir(parents=True)
        db.create_board(live)
        elsewhere = {"CAPSULE_HOME": tempfile.mkdtemp()}
        with mock.patch.dict(os.environ, elsewhere), \
                mock.patch.object(Path, "home", return_value=fake_user):
            with self.assertRaises(ValueError):
                replay._refuse_live(live)

    def test_a_case_variant_of_a_live_board_path_is_refused(self):
        home, live = self._live()
        variant = Path(str(live).replace("/boards/", "/BOARDS/"))
        if not variant.exists():
            self.skipTest("case-sensitive filesystem: a case variant is a different directory")
        with self.assertRaises(ValueError):
            replay._refuse_live(variant, home=home)


if __name__ == "__main__":
    unittest.main()
