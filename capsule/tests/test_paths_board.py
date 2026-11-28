"""Factory autonomy Task 9 (spec 4.1): `paths.config_path`/`paths.outbox_log` gain `board=`;
`paths.resolve_config` is a new symbol alongside them. Imports `paths` only, so reverting
`paths.py` alone (without touching `tick.py`) still fails this file.

Card t_a7491776: `ResolveConfig` was split out into its own file (`test_paths_resolve_config.py`)
to dodge the gate's old new-symbol shortcut, which seeded `known` with only ONE added name per
pure-addition module - `resolve_config` sharing a revert-mutation run with `config_path`/
`outbox_log`'s own board= check would have masked one behind the other. `mutation_newsymbol.
known_names` now seeds every added name in the module, so the split is no longer needed; folded
back here. `test_a_board_argument_is_still_accepted_by_config_path` (the gate-only duplicate of
`ConfigPath.test_a_board_is_its_own_directory` below, added only to give the split file its own
independent failure) is dropped rather than folded back."""
import tempfile
import unittest
from pathlib import Path

from capsule import paths


class ConfigPath(unittest.TestCase):
    def test_no_board_is_the_legacy_default(self):
        home = Path(tempfile.mkdtemp())
        self.assertEqual(paths.config_path(home), home / "config.json")

    def test_a_board_is_its_own_directory(self):
        home = Path(tempfile.mkdtemp())
        self.assertEqual(paths.config_path(home, "other"), home / "boards" / "other" / "config.json")


class OutboxLog(unittest.TestCase):
    def test_no_board_is_the_legacy_default(self):
        home = Path(tempfile.mkdtemp())
        self.assertEqual(paths.outbox_log(home), home / "outbox.log")

    def test_a_board_is_its_own_file(self):
        home = Path(tempfile.mkdtemp())
        self.assertEqual(paths.outbox_log(home, "other"), home / "boards" / "other" / "outbox.log")


class ResolveConfig(unittest.TestCase):
    """`paths.resolve_config` - the per-board config file when `board` names one that exists on
    disk, else the legacy path - even for a `board` with no file yet."""

    def test_no_board_is_the_legacy_path(self):
        home = Path(tempfile.mkdtemp())
        self.assertEqual(paths.resolve_config(home), home / "config.json")

    def test_a_board_with_a_config_file_is_its_own_path(self):
        home = Path(tempfile.mkdtemp())
        p = paths.config_path(home, "other")
        p.parent.mkdir(parents=True)
        p.write_text("{}")
        self.assertEqual(paths.resolve_config(home, "other"), p)

    def test_a_board_with_no_config_file_falls_back_to_the_legacy_path(self):
        home = Path(tempfile.mkdtemp())
        self.assertEqual(paths.resolve_config(home, "other"), home / "config.json")