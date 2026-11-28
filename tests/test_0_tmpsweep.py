"""Sorts first, so discovery imports it before any module-level mkdtemp (t_b346fd02)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from capsule.tests import tmpclean  # noqa: E402

tmpclean.install()
