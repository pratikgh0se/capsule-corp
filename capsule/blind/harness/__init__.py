"""The blind harness API (blind spec 3.3). Tests drive the product only through `run`, `read` and
`tmp`; the tick copies this package into the run snapshot as `/blind/harness` and imports it as
`harness` (PYTHONPATH=/blind, cwd=/blind), so it is stdlib-only and uses relative imports.
Protected path: a worker never edits the code that judges it."""
from .api import read, run, tmp

__all__ = ["read", "run", "tmp"]
