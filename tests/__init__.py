"""A single-module run (`-m unittest <pkg>.test_x`) imports this package but never the sorted-first
test_0_tmpsweep.py stub, so its mkdtemp dirs stayed in the real $TMPDIR (~800 per
test_board_bridge run). Install the same private, swept root `discover` gets."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from capsule.tests import tmpclean  # noqa: E402

tmpclean.install()
